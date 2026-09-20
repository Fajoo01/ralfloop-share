# MD / Goodify Android static reverse engineering — 2026-09-20

## Scope and safety

Static analysis only. No donation, purchase, QR redemption, or Goodify transaction was submitted. The paired Android phone was not needed for this pass.

APK under analysis:

- path: `/tmp/md-latest.apk`
- package: `com.agora.md`
- SHA-256: `3646bd7530718a41e168b791f7dd27587321f4a858bda71d9402ab612799fd44`

This note intentionally omits unrelated embedded keys/secrets found in the APK.

## Goodify endpoints and request flow

The production Goodify endpoints referenced by the APK are:

- `POST https://catalogomdapp.dedagroupwiz.it/api/goodify/getdonation`
- `POST https://catalogomdapp.dedagroupwiz.it/api/goodify/purchasedonation`

`api.platform-backend.mdspa.it` is used by the app for login/auth flows, not for these two Goodify endpoints.

### Donation history

`introGoodifyFragment.getlistDonation()` builds this JSON body:

```json
{
  "token": "<Costanti.staticBeans.accessToken>"
}
```

It calls `CallManager.MethodPostJsonBody(...)` and `Engine.getUrlListDonation()`.

### QR purchase/donation start

The scanner flow is:

`ScannerGoodifyActivity` -> decoded QR -> `HomePage_Activity.startPurchaseDonation(qr)` -> `purchasedonation`.

The JSON body is:

```json
{
  "token": "<Costanti.staticBeans.accessToken>",
  "qr_code": "<decoded QR>"
}
```

The overload used by both Goodify calls invokes `MethodPOST_JSON(url, callback, body)` without a custom header map. Therefore this code path does **not** add an app-defined `Authorization: Bearer ...` header; the Goodify token is carried in the JSON body. Volley still handles its normal JSON request metadata.

## Response contracts consumed by the APK

Both callbacks receive a response string and construct a top-level `JSONObject`.

### `getdonation`

The app consumes this structure:

```json
{
  "code": "",
  "messaggio": "...",
  "payload": [
    {
      "Donation": [
        { "...": "itemDonazione fields below" }
      ]
    }
  ]
}
```

Behavior:

- a non-empty `code` is treated as an error and `messaggio` is shown;
- on success the app reads `payload[0].Donation`;
- every object in `Donation` is deserialized with Gson into `itemDonazione`;
- the resulting list becomes `Costanti$arrayList.elencoDonazioni`.

The current APK's `itemDonazione` bean has these exact Gson `SerializedName` mappings, all represented as Java `String` fields:

| Java field | JSON field |
| --- | --- |
| `codiceNegozio` | `codice_negozio` |
| `codiceOperatore` | `codice_operatore` |
| `dataTransazione` | `data_transazione` |
| `dateQrlockedLocked` | `date_qrlocked_locked` |
| `goodifyAssociationName` | `Goodify_associationName` |
| `goodifyDateDonation` | `Goodify_date_donation` |
| `goodifyDonatedAmount` | `Goodify_donatedAmount` |
| `goodifyDonatedValuta` | `Goodify_donatedValuta` |
| `goodifyDonation` | `Goodify_donation` |
| `goodifyDonationId` | `Goodify_donationId` |
| `goodifyUrldonationId` | `Goodify_UrldonationId` |
| `idCassa` | `id_cassa` |
| `idTransazione` | `id_transazione` |
| `importoSpeso` | `importo_speso` |
| `intLockedVerify` | `intLockedVerify` |

### `purchasedonation`

The minimal response shape actually consumed by `HomePage_Activity$4.onSuccess` is:

```json
{
  "messaggio": "...",
  "payload": [
    {
      "Donation": [
        {
          "Goodify_associationName": "...",
          "Goodify_UrldonationId": "..."
        }
      ]
    }
  ]
}
```

The handler reads only the first `payload` item and first `Donation` item. It logs the association name and opens `Goodify_UrldonationId` in the app's `WebFragment`.

Important consequence: posting the QR to `purchasedonation` does not, by itself, prove a completed donation. The endpoint returns data that leads the user into a Goodify web flow.

## accessToken provenance and persistence

`Costanti$staticBeans.accessToken` is the ordinary MD login access token; there is no separate Goodify token found in this flow.

### Acquisition

Normal login uses:

- endpoint: `POST https://api.platform-backend.mdspa.it/auth/login`
- JSON body fields: `Email`, `Password`
- app login response handler parses the response into `itemLogin`, takes the first payload user object, obtains its access token, and assigns it to `Costanti$staticBeans.accessToken`.

The no-token login header builder also supplies device/app metadata and an API-key resource. The API-key value is intentionally not copied into this repository because it is unnecessary for documenting Goodify behavior.

The refresh path uses `POST https://api.platform-backend.mdspa.it/auth/refreshtoken` with `accessToken`, `refreshToken`, and `deviceId`, then replaces the stored access/refresh tokens from the response.

### Persistence

At application startup the app creates its preference manager and rehydrates the static token from persistent storage.

For the production build:

- SharedPreferences file: `MD_PREF_MANAGER`
- key: `accessToken`
- mode: `Context.MODE_PRIVATE`
- write primitive: `SharedPreferences.Editor.putString(...).commit()`
- read primitive: `SharedPreferences.getString(key, "")`

No application-layer encryption is present in this `PreferenceManager` wrapper. This means the value is plaintext inside the app-private Android SharedPreferences sandbox; normal Android app sandbox permissions still apply.

## Corrections to preliminary analysis

Two preliminary conclusions were too broad and are corrected here:

1. The current `itemDonazione` class is a flat 15-field Gson model. An earlier broad DEX scan mixed in an unrelated donation-shaped model.
2. The Goodify `MethodPostJsonBody` path does not install a custom Bearer header. The token for these two calls is in the JSON body.

## Reproducible extractor

Use:

```bash
python scripts/reverse_md_goodify_apk.py --dex-dir /tmp/md-apk
```

The script deliberately prints only Goodify endpoint/method references, access-token method references, and `itemDonazione` Gson mappings. It does not dump the APK string table or unrelated embedded credentials.

Supporting local analysis files from this session include:

- `/tmp/md-goodify-decompiled2.txt`
- `/tmp/md-goodify-targeted-re.txt`
- `/tmp/md-goodify-token-storage.txt`
- `/tmp/md-goodify-login-api.txt`
- `/tmp/md-goodify-login-details.txt`
- `/tmp/md-goodify-prefname.txt`
- `/tmp/md-callmanager-postjson.txt`

## Next safe implementation step

Before any live transaction code, keep the MCP transactional boundary explicit:

- parse and expose `getdonation` read-only responses;
- provide a local QR payload validator/builder that does not POST;
- keep `purchasedonation` network submission disabled by default;
- if live protocol validation is later needed, observe only a user-driven flow and avoid completing the Goodify web transaction.
