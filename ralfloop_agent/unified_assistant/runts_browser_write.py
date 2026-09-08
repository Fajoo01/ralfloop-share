from __future__ import annotations

import json
import os
from pathlib import Path
import time
from collections import deque
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


class RuntsBrowserWriteError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        phase: str | None = None,
        writes: int = 0,
    ) -> None:
        self.reason = str(reason)
        self.phase = phase
        self.writes = max(0, int(writes))
        super().__init__(self.reason)


class RuntsAuthenticatedCdpWriteTransport:
    """
    Narrow RUNTS Messaggistica writer.

    This class does NOT expose an arbitrary HTTP method or URL.
    The only mutation implemented is the already observed official
    frontend workflow:

      exact selected practice
      -> B00
      -> upload one approved PDF
      -> exact subject/body
      -> Invia
      -> CONFERMA

    preflight() never performs a mutation.
    execute() is the only mutating entrypoint.
    """

    HOST = "runts.lavoro.gov.it"

    ISTA_HOST = "ista.scrivaniapa.infocamere.it"
    CORE_HOST = "api.terzosettore.infocamere.it"

    MAX_FILE_SIZE = 10_244_588

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:9236",
        *,
        timeout_s: float = 20.0,
    ) -> None:
        parsed = urlparse(endpoint.rstrip("/"))

        if (
            parsed.scheme != "http"
            or parsed.hostname
            not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError(
                "runts_cdp_endpoint_must_be_loopback"
            )

        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = float(timeout_s)

    # --------------------------------------------------------
    # PUBLIC TYPED API
    # --------------------------------------------------------

    def preflight(
        self,
        scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._validate_scope(scope)

        import websocket

        page = self._page()

        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"],
            timeout=2,
        )

        events = deque()
        counter = 0

        def call(method, params=None):
            nonlocal counter

            counter += 1
            ident = counter

            ws.send(json.dumps({
                "id": ident,
                "method": method,
                "params": params or {},
            }))

            while True:
                row = json.loads(ws.recv())

                if row.get("id") == ident:
                    if row.get("error"):
                        raise RuntsBrowserWriteError(
                            "runts_cdp_call_failed"
                        )

                    result = row.get("result", {})

                    if (
                        method == "Runtime.evaluate"
                        and result.get(
                            "exceptionDetails"
                        )
                    ):
                        raise RuntsBrowserWriteError(
                            "runts_javascript_failed"
                        )

                    return result

                if row.get("method"):
                    events.append(row)

        try:
            call("Network.enable", {
                "maxTotalBufferSize": 30_000_000,
                "maxResourceBufferSize": 15_000_000,
            })

            snapshot = self._snapshot(
                call,
                str(scope["practice_id"]),
            )

            if not snapshot["session_valid"]:
                return {
                    "ok": False,
                    "reason": "runts_session_invalid",
                    "writes": 0,
                }

            if (
                snapshot["selected_practice_id"]
                != str(scope["practice_id"])
            ):
                return {
                    "ok": False,
                    "reason":
                        "runts_selected_practice_mismatch",
                    "selected_practice_id":
                        snapshot[
                            "selected_practice_id"
                        ],
                    "writes": 0,
                }

            if snapshot["b00_count"] != 1:
                return {
                    "ok": False,
                    "reason": "runts_b00_unresolved",
                    "writes": 0,
                }

            missing = [
                name
                for name, value in {
                    "subject_input":
                        snapshot["subject_input"],
                    "body_input":
                        snapshot["body_input"],
                    "file_input":
                        snapshot["file_input"],
                    "send_button":
                        snapshot["send_button"],
                }.items()
                if not value
            ]

            if missing:
                return {
                    "ok": False,
                    "reason":
                        "runts_message_controls_missing",
                    "missing": missing,
                    "writes": 0,
                }

            return {
                "ok": True,
                "writes": 0,
                "document_type_code": "B00",
                "selected_practice_id":
                    snapshot[
                        "selected_practice_id"
                    ],
                "session_valid": True,
                "message_controls_ready": True,
                "transport":
                    "authenticated_runts_frontend",
            }

        finally:
            ws.close()

    def execute(
        self,
        scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """
        First mutation: PDF upload.
        Second mutation: final practice message submit.

        Caller MUST already hold the approval execution claim.
        """
        self._validate_scope(scope)

        if os.getenv(
            "BOTTAZZI_RUNTS_WRITE_ENABLED",
            "0",
        ) != "1":
            raise RuntsBrowserWriteError(
                "runts_write_feature_disabled"
            )

        import websocket

        page = self._page()

        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"],
            timeout=2,
        )

        events = deque()
        counter = 0

        # Diagnostic state. "writes" counts only provider
        # mutations confirmed by a successful HTTP response.
        phase = "connect"
        writes = 0

        def call(method, params=None):
            nonlocal counter

            counter += 1
            ident = counter

            ws.send(json.dumps({
                "id": ident,
                "method": method,
                "params": params or {},
            }))

            while True:
                row = json.loads(ws.recv())

                if row.get("id") == ident:
                    if row.get("error"):
                        raise RuntsBrowserWriteError(
                            "runts_cdp_call_failed"
                        )

                    result = row.get("result", {})

                    if (
                        method == "Runtime.evaluate"
                        and result.get(
                            "exceptionDetails"
                        )
                    ):
                        raise RuntsBrowserWriteError(
                            "runts_javascript_failed"
                        )

                    return result

                if row.get("method"):
                    events.append(row)

        try:
            phase = "network_enable"

            call("Network.enable", {
                "maxTotalBufferSize": 30_000_000,
                "maxResourceBufferSize": 15_000_000,
            })

            # Recheck immediately before first mutation.
            phase = "preflight"

            snap = self._snapshot(
                call,
                str(scope["practice_id"]),
            )

            if (
                not snap["session_valid"]
                or snap["selected_practice_id"]
                != str(scope["practice_id"])
                or snap["b00_count"] != 1
            ):
                raise RuntsBrowserWriteError(
                    "runts_execute_preflight_changed"
                )

            # Open the attachment modal exactly as the user
            # does. The B00 bootstrap-select lives in this modal
            # and must be active before selecting the document type.
            phase = "attachment_modal"

            attachment_modal = self._evaluate_value(
                call,
                r"""
                (async () => {
                  const visible = el =>
                    !!(
                      el &&
                      (
                        el.offsetWidth ||
                        el.offsetHeight ||
                        el.getClientRects().length
                      )
                    );

                  const normalize = value =>
                    String(value || '')
                      .replace(/\s+/g, ' ')
                      .trim()
                      .toLowerCase();

                  const modal =
                    document.querySelector(
                      '#modaleAggiungiAllegato'
                    );

                  if (visible(modal)) {
                    return {
                      ok:true,
                      alreadyOpen:true
                    };
                  }

                  const buttons =
                    Array.from(
                      document.querySelectorAll('button')
                    ).filter(
                      button =>
                        visible(button)
                        && normalize(
                          button.innerText
                          || button.textContent
                        ) === 'carica allegato'
                    );

                  if (buttons.length !== 1) {
                    return {
                      ok:false,
                      reason:
                        'attachment_button_ambiguous',
                      count:buttons.length
                    };
                  }

                  buttons[0].click();

                  // Bootstrap's fade transition is asynchronous.
                  // Poll the actual modal state instead of relying
                  // on a fixed animation delay.
                  const deadline =
                    performance.now() + 2000;

                  while (
                    !visible(modal)
                    && performance.now() < deadline
                  ) {
                    await new Promise(
                      resolve => setTimeout(resolve, 50)
                    );
                  }

                  return {
                    ok:visible(modal),
                    alreadyOpen:false
                  };
                })()
                """,
            )

            if not (
                isinstance(attachment_modal, dict)
                and attachment_modal.get("ok") is True
            ):
                raise RuntsBrowserWriteError(
                    "runts_attachment_modal_unavailable"
                )

            phase = "select_b00"

            self._fill_message_and_select_b00(
                call,
                str(scope["subject"]),
                str(scope["body"]),
            )

            # ------------------------------------------------
            # MUTATION 1: official Angular fileProgress()
            # ------------------------------------------------

            phase = "upload_input"

            document = call(
                "DOM.getDocument",
                {"depth": -1, "pierce": True},
            )

            root_id = int(
                document["root"]["nodeId"]
            )

            input_node = call(
                "DOM.querySelector",
                {
                    "nodeId": root_id,
                    "selector": "#upload4",
                },
            ).get("nodeId")

            if not input_node:
                raise RuntsBrowserWriteError(
                    "runts_file_input_missing"
                )

            call(
                "DOM.setFileInputFiles",
                {
                    "nodeId": int(input_node),
                    "files": [
                        str(
                            Path(
                                str(scope["pdf_path"])
                            ).resolve()
                        )
                    ],
                },
            )

            # Explicitly fire the Angular change handler.
            phase = "upload_dispatch"

            call(
                "Runtime.evaluate",
                {
                    "expression": r"""
                    (() => {
                      const el =
                        document.querySelector('#upload4');

                      if (!el) return false;

                      el.dispatchEvent(
                        new Event(
                          'change',
                          {bubbles:true}
                        )
                      );

                      return true;
                    })()
                    """,
                    "returnByValue": True,
                },
            )

            phase = "upload_wait"

            upload = self._wait_post(
                ws,
                call,
                events,
                host=self.ISTA_HOST,
                path=(
                    "/api/v1/messaggio/"
                    "allegato/istanza/"
                    + str(scope["practice_id"])
                ),
            )

            # _wait_post returned a successful provider
            # response: mutation 1 is now confirmed.
            writes = 1
            phase = "upload_validate"

            self._validate_upload(
                upload,
                scope,
            )

            upload_body = upload["body"]

            document_row = upload_body.get(
                "messaggioDocumento"
            )

            upload_document_id = str(
                (
                    document_row or {}
                ).get(
                    "idMessaggioDocumento"
                )
                or (
                    document_row or {}
                ).get("idStorage")
                or ""
            )

            # Let the Angular subscribe callback place
            # messaggioDocumento into messaggioDocumentos.
            time.sleep(0.5)

            # ------------------------------------------------
            # Open final confirmation modal.
            # ------------------------------------------------

            phase = "send_click"

            opened = self._evaluate_value(
                call,
                r"""
                (() => {
                  const visible = el =>
                    !!(
                      el.offsetWidth ||
                      el.offsetHeight ||
                      el.getClientRects().length
                    );

                  const buttons =
                    Array.from(
                      document.querySelectorAll(
                        'button'
                      )
                    )
                    .filter(
                      b =>
                        String(
                          b.innerText || ''
                        ).trim() === 'Invia'
                        && visible(b)
                    );

                  if (buttons.length !== 1) {
                    return {
                      ok:false,
                      count:buttons.length
                    };
                  }

                  buttons[0].click();

                  return {ok:true};
                })()
                """,
            )

            if not (
                isinstance(opened, dict)
                and opened.get("ok") is True
            ):
                raise RuntsBrowserWriteError(
                    "runts_send_button_ambiguous"
                )

            time.sleep(0.25)

            phase = "confirm_click"

            confirmed = self._evaluate_value(
                call,
                r"""
                (() => {
                  const visible = el =>
                    !!(
                      el.offsetWidth ||
                      el.offsetHeight ||
                      el.getClientRects().length
                    );

                  const buttons =
                    Array.from(
                      document.querySelectorAll(
                        '#modaleConfermaInvio.show button,'
                        + '#modaleConfermaInvio.show '
                        + 'input[type=button],'
                        + '#modaleConfermaInvio.show '
                        + 'input[type=submit]'
                      )
                    )
                    .filter(el => {
                      const label =
                        String(
                          el.innerText
                          || el.value
                          || ''
                        ).trim();

                      return (
                        label === 'CONFERMA'
                        && visible(el)
                      );
                    });

                  if (buttons.length !== 1) {
                    return {
                      ok:false,
                      count:buttons.length
                    };
                  }

                  buttons[0].click();

                  return {ok:true};
                })()
                """,
            )

            if not (
                isinstance(confirmed, dict)
                and confirmed.get("ok") is True
            ):
                # Upload already happened: this is a
                # partial write and must never be retried
                # automatically by the caller.
                raise RuntsBrowserWriteError(
                    "runts_confirmation_button_ambiguous"
                )

            # ------------------------------------------------
            # MUTATION 2: send()
            # ------------------------------------------------

            phase = "send_wait"

            submitted = self._wait_post(
                ws,
                call,
                events,
                host=self.CORE_HOST,
                path=(
                    "/api/v1/messaggio/"
                    "istanza/"
                    + str(scope["practice_id"])
                    + "/pec"
                ),
            )

            # Final provider POST returned successfully.
            writes = 2
            phase = "submit_validate"

            self._validate_submit(
                submitted,
                scope,
                upload_document_id,
            )

            return {
                "status":
                    "provider_submit_accepted",
                "writes": 2,
                "upload_document_id":
                    upload_document_id,
                "upload_ret_code":
                    int(
                        upload_body.get(
                            "retCode"
                        )
                    ),
                "submit_ret_code":
                    int(
                        submitted["body"].get(
                            "retCode"
                        )
                    ),
            }

        except RuntsBrowserWriteError as exc:
            # Preserve the original narrow error reason while
            # attaching the phase and confirmed mutation count.
            if exc.phase is None:
                exc.phase = phase

            if exc.writes < writes:
                exc.writes = writes

            raise

        except Exception as exc:
            # Preserve phase/count without persisting arbitrary
            # third-party exception text, which may contain
            # URLs, headers or session details.
            raise RuntsBrowserWriteError(
                type(exc).__name__,
                phase=phase,
                writes=writes,
            ) from exc

        finally:
            ws.close()

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    def _validate_scope(
        self,
        scope: Mapping[str, Any],
    ) -> None:
        practice = str(
            scope.get("practice_id") or ""
        )

        if (
            not practice.isdecimal()
            or not 1 <= len(practice) <= 24
        ):
            raise ValueError(
                "runts_practice_id_invalid"
            )

        if (
            str(
                scope.get(
                    "document_type_code"
                ) or ""
            )
            != "B00"
        ):
            raise ValueError(
                "runts_document_type_denied"
            )

        if (
            str(scope.get("channel") or "")
            != "RUNTS MESSAGGISTICA"
        ):
            raise ValueError(
                "runts_channel_denied"
            )

        if scope.get("no_new_deposit") is not True:
            raise ValueError(
                "runts_new_deposit_denied"
            )

        subject = str(
            scope.get("subject") or ""
        )

        body = str(
            scope.get("body") or ""
        )

        if (
            not subject
            or len(subject) > 500
            or not body
            or len(body) > 4000
        ):
            raise ValueError(
                "runts_message_shape_invalid"
            )

        path = Path(
            str(scope.get("pdf_path") or "")
        )

        if (
            not path.is_file()
            or path.suffix.casefold() != ".pdf"
            or path.stat().st_size
            > self.MAX_FILE_SIZE
        ):
            raise ValueError(
                "runts_pdf_shape_invalid"
            )

    def _page(self) -> dict[str, Any]:
        try:
            with urlopen(
                self.endpoint + "/json/list",
                timeout=self.timeout_s,
            ) as response:
                rows = json.load(response)

        except Exception as exc:
            raise RuntsBrowserWriteError(
                "runts_cdp_inventory_unavailable"
            ) from exc

        matches = [
            row
            for row in rows
            if row.get("type") == "page"
            and urlparse(
                str(row.get("url") or "")
            ).hostname == self.HOST
            and urlparse(
                str(row.get("url") or "")
            ).path.startswith(
                "/frontoffice/"
            )
        ]

        if len(matches) != 1:
            raise RuntsBrowserWriteError(
                "authenticated_runts_page_unresolved"
            )

        return matches[0]

    def _snapshot(
        self,
        call,
        practice_id: str,
    ) -> dict[str, Any]:
        value = self._evaluate_value(
            call,
            r"""
            (() => {
              function visible(el) {
                return !!(
                  el &&
                  (
                    el.offsetWidth ||
                    el.offsetHeight ||
                    el.getClientRects().length
                  )
                );
              }

              let user = null;

              try {
                user = JSON.parse(
                  sessionStorage.getItem(
                    'auth-user-info_TSFO'
                  )
                );
              } catch (_) {}

              const select =
                document.querySelector('select#tipoDoc');

              const options =
                select
                ? Array.from(select.options)
                : [];

              const b00 =
                options.filter(
                  o =>
                    String(
                      o.textContent || ''
                    ).includes('(B00)')
                );

              const resources =
                performance
                  .getEntriesByType('resource')
                  .map(x => x.name)
                  .filter(url => {
                    try {
                      const u = new URL(url);

                      return (
                        u.hostname ===
                          'ista.scrivaniapa.infocamere.it'
                        &&
                        /^\/api\/v1\/messaggio\/\d+$/
                          .test(u.pathname)
                      );
                    } catch (_) {
                      return false;
                    }
                  });

              let selected = '';

              if (resources.length) {
                const u =
                  new URL(
                    resources[
                      resources.length - 1
                    ]
                  );

                selected =
                  u.pathname.split('/').pop();
              }

              const subject =
                document.querySelector(
                  'input[name="oggettoMessaggi"]'
                )
                || document.querySelector(
                  '#oggettoMessaggi'
                );

              const body =
                document.querySelector(
                  'textarea[name="testoMessaggi"]'
                )
                || document.querySelector(
                  '#testoMessaggi'
                );

              const file =
                document.querySelector(
                  '#upload4'
                );

              const send =
                Array.from(
                  document.querySelectorAll(
                    'button'
                  )
                )
                .filter(
                  b =>
                    String(
                      b.innerText || ''
                    ).trim() === 'Invia'
                    && visible(b)
                );

              return {
                session_valid: !!(
                  user &&
                  user.idUtente !== undefined &&
                  user.idUtente !== null
                ),
                selected_practice_id:
                  selected,
                b00_count: b00.length,
                subject_input: !!subject,
                body_input: !!body,
                file_input: !!file,
                send_button:
                  send.length === 1
              };
            })()
            """,
        )

        if not isinstance(value, dict):
            raise RuntsBrowserWriteError(
                "runts_snapshot_invalid"
            )

        return value

    def _fill_message_and_select_b00(
        self,
        call,
        subject: str,
        body: str,
    ) -> None:
        value = self._evaluate_value(
            call,
            """
            (() => {
              const subject =
                document.querySelector(
                  'input[name="oggettoMessaggi"]'
                )
                || document.querySelector(
                  '#oggettoMessaggi'
                );

              const body =
                document.querySelector(
                  'textarea[name="testoMessaggi"]'
                )
                || document.querySelector(
                  '#testoMessaggi'
                );

              const select =
                document.querySelector('select#tipoDoc');

              if (
                !subject ||
                !body ||
                !select
              ) {
                return {
                  ok:false,
                  reason:'controls_missing'
                };
              }

              const options =
                Array.from(select.options);

              const matches =
                options
                  .map((o, i) => [o, i])
                  .filter(
                    ([o]) =>
                      String(
                        o.textContent || ''
                      ).includes('(B00)')
                  );

              if (matches.length !== 1) {
                return {
                  ok:false,
                  reason:'b00_ambiguous'
                };
              }

              function setValue(
                el,
                value
              ) {
                const proto =
                  el instanceof
                    HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;

                const setter =
                  Object.getOwnPropertyDescriptor(
                    proto,
                    'value'
                  ).set;

                setter.call(el, value);

                el.dispatchEvent(
                  new Event(
                    'input',
                    {bubbles:true}
                  )
                );

                el.dispatchEvent(
                  new Event(
                    'change',
                    {bubbles:true}
                  )
                );
              }

              setValue(
                subject,
                %s
              );

              setValue(
                body,
                %s
              );

              // bootstrap-select is the real UI control.
              // Interact with it as the user does instead of
              // mutating selectedIndex behind Angular's back.
              const wrapper =
                select.closest('.bootstrap-select');

              if (!wrapper) {
                return {
                  ok:false,
                  reason:'b00_bootstrap_missing'
                };
              }

              const toggle =
                wrapper.querySelector(
                  'button.dropdown-toggle[data-id="tipoDoc"]'
                );

              if (!toggle) {
                return {
                  ok:false,
                  reason:'b00_bootstrap_toggle_missing'
                };
              }

              const normalize = value =>
                String(value || '')
                  .replace(/\\s+/g, ' ')
                  .trim();

              const expected =
                "BILANCIO D'ESERCIZIO (B00)";

              // Open bootstrap-select first. It may rebuild the
              // dropdown option nodes while opening, so the B00
              // node MUST be queried only after the menu is open.
              toggle.click();

              return new Promise(resolve => {
                setTimeout(() => {
                  const uiMatches =
                    Array.from(
                      wrapper.querySelectorAll(
                        '.dropdown-menu.show '
                        + 'a[role="option"]'
                      )
                    ).filter(
                      el =>
                        normalize(
                          el.innerText
                          || el.textContent
                        ) === expected
                    );

                  if (uiMatches.length !== 1) {
                    resolve({
                      ok:false,
                      reason:
                        'b00_bootstrap_option_ambiguous',
                      count:uiMatches.length
                    });
                    return;
                  }

                  // Click the live option node generated by
                  // bootstrap-select.
                  uiMatches[0].click();

                  setTimeout(() => {
                    const selectedOption =
                      select.selectedIndex >= 0
                        ? select.options[
                            select.selectedIndex
                          ]
                        : null;

                    const selected =
                      normalize(
                        selectedOption
                          ? selectedOption.textContent
                          : ''
                      );

                    // Read visible button text first. data-title
                    // can represent the placeholder before the
                    // bootstrap state has settled.
                    const bootstrapTitle =
                      normalize(
                        toggle.innerText
                        || toggle.textContent
                        || toggle.getAttribute(
                          'data-title'
                        )
                      );

                    // Re-query selected UI state after the click;
                    // do not rely on the node captured before it.
                    const selectedUi =
                      Array.from(
                        wrapper.querySelectorAll(
                          'a[role="option"].selected,'
                          + 'a[role="option"]'
                          + '[aria-selected="true"]'
                        )
                      ).filter(
                        el =>
                          normalize(
                            el.innerText
                            || el.textContent
                          ) === expected
                      );

                    const uiSelected =
                      selectedUi.length === 1;

                    const ok =
                      selected === expected
                      && bootstrapTitle === expected
                      && uiSelected;

                    resolve({
                      ok,
                      selected,
                      bootstrapTitle,
                      uiSelected,
                      reason:
                        ok
                          ? null
                          : 'b00_bootstrap_binding_failed'
                    });
                  }, 300);
                }, 150);
              });
            })()
            """
            % (
                json.dumps(subject),
                json.dumps(body),
            ),
        )

        if not (
            isinstance(value, dict)
            and value.get("ok") is True
            and "(B00)" in str(
                value.get("selected") or ""
            )
        ):
            raise RuntsBrowserWriteError(
                "runts_form_binding_failed"
            )

    def _evaluate_value(
        self,
        call,
        expression: str,
    ):
        result = call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )

        return (
            result.get("result", {})
            .get("value")
        )

    def _wait_post(
        self,
        ws,
        call,
        events,
        *,
        host: str,
        path: str,
    ) -> dict[str, Any]:
        import websocket

        deadline = (
            time.monotonic()
            + self.timeout_s
        )

        requests: dict[
            str,
            dict[str, Any],
        ] = {}

        statuses: dict[str, int] = {}

        while time.monotonic() < deadline:
            try:
                row = (
                    events.popleft()
                    if events
                    else json.loads(
                        ws.recv()
                    )
                )

            except websocket.WebSocketTimeoutException:
                continue

            method = row.get("method")
            params = row.get(
                "params",
                {},
            )

            request_id = str(
                params.get("requestId")
                or ""
            )

            if (
                method
                == "Network.requestWillBeSent"
            ):
                request = params.get(
                    "request",
                    {},
                )

                parsed = urlparse(
                    str(
                        request.get("url")
                        or ""
                    )
                )

                if (
                    request.get("method")
                    == "POST"
                    and parsed.hostname == host
                    and parsed.path == path
                ):
                    requests[
                        request_id
                    ] = {
                        "url":
                            request.get("url"),
                        "postData":
                            request.get("postData"),
                    }

            elif (
                method
                == "Network.responseReceived"
                and request_id in requests
            ):
                status = int(
                    params.get(
                        "response",
                        {},
                    ).get(
                        "status"
                    )
                    or 0
                )

                statuses[
                    request_id
                ] = status

                if status == 401:
                    raise RuntsBrowserWriteError(
                        "runts_session_expired"
                    )

            elif (
                method
                == "Network.loadingFinished"
                and request_id in requests
            ):
                status = statuses.get(
                    request_id,
                    0,
                )

                if not 200 <= status < 300:
                    raise RuntsBrowserWriteError(
                        "runts_provider_http_error"
                    )

                result = call(
                    "Network.getResponseBody",
                    {
                        "requestId":
                            request_id
                    },
                )

                try:
                    body = json.loads(
                        result.get(
                            "body",
                            "",
                        )
                    )

                except json.JSONDecodeError as exc:
                    raise RuntsBrowserWriteError(
                        "runts_provider_response_malformed"
                    ) from exc

                if not isinstance(
                    body,
                    dict,
                ):
                    raise RuntsBrowserWriteError(
                        "runts_provider_response_shape_invalid"
                    )

                return {
                    **requests[
                        request_id
                    ],
                    "status": status,
                    "body": body,
                }

        raise RuntsBrowserWriteError(
            "runts_provider_write_timeout"
        )

    def _validate_upload(
        self,
        observed: Mapping[str, Any],
        scope: Mapping[str, Any],
    ) -> None:
        body = observed["body"]

        if int(
            body.get("retCode", -1)
        ) != 0:
            raise RuntsBrowserWriteError(
                "runts_upload_rejected"
            )

        document = body.get(
            "messaggioDocumento"
        )

        if not isinstance(
            document,
            dict,
        ):
            raise RuntsBrowserWriteError(
                "runts_upload_document_missing"
            )

        parsed = urlparse(
            str(
                observed.get("url")
                or ""
            )
        )

        query = parse_qs(
            parsed.query
        )

        if query.get(
            "app"
        ) not in (
            ["TSFO"],
            ["tsfo"],
        ):
            raise RuntsBrowserWriteError(
                "runts_upload_app_mismatch"
            )

        if query.get(
            "validaFile"
        ) not in (
            ["true"],
            ["True"],
        ):
            raise RuntsBrowserWriteError(
                "runts_upload_validation_missing"
            )

        if not query.get(
            "idUtente"
        ):
            raise RuntsBrowserWriteError(
                "runts_upload_user_missing"
            )

        tipo = query.get(
            "tipoDocumento"
        )

        if (
            not tipo
            or tipo[0]
            not in {
                "application/pdf",
                "application/octet-stream",
            }
        ):
            raise RuntsBrowserWriteError(
                "runts_upload_mime_unexpected"
            )

    def _validate_submit(
        self,
        observed: Mapping[str, Any],
        scope: Mapping[str, Any],
        upload_document_id: str,
    ) -> None:
        response = observed["body"]

        if int(
            response.get(
                "retCode",
                -1,
            )
        ) != 0:
            raise RuntsBrowserWriteError(
                "runts_submit_rejected"
            )

        raw = observed.get(
            "postData"
        )

        if not isinstance(
            raw,
            str,
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_body_unobserved"
            )

        try:
            payload = json.loads(
                raw
            )

        except json.JSONDecodeError as exc:
            raise RuntsBrowserWriteError(
                "runts_submit_body_malformed"
            ) from exc

        message = payload.get(
            "messaggio"
        )

        if not isinstance(
            message,
            dict,
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_message_missing"
            )

        if str(
            message.get("oggetto")
            or ""
        ) != str(
            scope["subject"]
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_subject_drift"
            )

        if str(
            message.get("corpo")
            or ""
        ) != str(
            scope["body"]
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_body_drift"
            )

        if str(
            message.get("nota")
            or ""
        ) != "":
            raise RuntsBrowserWriteError(
                "runts_submit_note_unexpected"
            )

        docs = message.get(
            "messaggioDocumentos"
        )

        if (
            not isinstance(
                docs,
                list,
            )
            or len(docs) != 1
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_attachment_count_invalid"
            )

        doc = docs[0]

        if not isinstance(
            doc,
            dict,
        ):
            raise RuntsBrowserWriteError(
                "runts_submit_attachment_shape_invalid"
            )

        deco = doc.get(
            "decoTipo"
        )

        if (
            not isinstance(
                deco,
                dict,
            )
            or not deco.get(
                "idDecodifica"
            )
        ):
            raise RuntsBrowserWriteError(
                "runts_b00_id_not_bound"
            )

        observed_id = str(
            doc.get(
                "idMessaggioDocumento"
            )
            or doc.get(
                "idStorage"
            )
            or ""
        )

        if (
            upload_document_id
            and observed_id
            != upload_document_id
        ):
            raise RuntsBrowserWriteError(
                "runts_attachment_identity_drift"
            )


__all__ = [
    "RuntsAuthenticatedCdpWriteTransport",
    "RuntsBrowserWriteError",
]
