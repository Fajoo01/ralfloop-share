# Shell execution boundary inventory

| Boundary | Agent-controlled shell string | Protection |
|---|---:|---|
| `openshell_backend.app.exec_in_sandbox` | yes | Canonical `RalfShellJudge` before `/bin/bash -lc` |
| `OpenShellAdapterStub.exec` | yes | Same judge before local Bash |
| `coding_harness._run_shell` validator | potentially | Same judge; `REVIEW` returns 126 without execution |
| repair workflow helpers | no; fixed argv assembled by trusted code | Outside shell-string scope; `shell=False` |
| MCP brokers/transports | no; fixed argv | Outside scope; `shell=False` |
| model/runtime process managers | no; fixed argv | Outside scope; `shell=False` |
| local architecture/test runners | no; trusted internal argv | Outside agent command boundary |
| remote-code sandbox launcher | code artifact, not shell command | Separate bubblewrap/seccomp boundary |

The legacy `PolicyLayer.check_command` and backend `check_command_allowed` remain only
as compatibility wrappers and delegate to `RalfShellJudge`; they contain no substring
security decision.
