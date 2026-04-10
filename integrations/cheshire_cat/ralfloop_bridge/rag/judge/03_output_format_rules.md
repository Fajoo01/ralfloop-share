
## shell execution
For shell command tasks:
- prefer commands that can run immediately
- use execution feedback if available
- reject commands that fail when a corrected version can be produced

## Strict shell output
If output_format is shell:
- return only shell commands
- no explanations
- no bullet points
- no markdown commentary outside the shell block
- no follow-up text

## Reject fake shell placeholders
For shell command tasks:
- reject placeholder paths like /path/to/directory unless explicitly requested
- prefer runnable commands that succeed in the current environment
- if the first shell attempt fails because of an invented placeholder path, correct it
