
## Python execution quality
For Python tasks:
- generate code that can run immediately
- if execution feedback is present, fix the concrete runtime error
- prefer runnable code over decorative code

## shell execution quality
For shell tasks:
- generate runnable commands
- if execution feedback is present, fix the concrete shell error
- prefer simple portable commands

## Strict shell commands
For shell command tasks:
- output only commands
- no explanations
- no introductory text
- no closing text

## Avoid fake shell placeholders
For shell command tasks:
- do not invent placeholder paths like /path/to/directory unless the user explicitly asks for an example placeholder
- prefer commands that can run immediately in the current environment
- if a command needs a path and no path was provided, prefer a safe runnable default like "." instead of a fake placeholder
