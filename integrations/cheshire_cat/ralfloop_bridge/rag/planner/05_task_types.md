# Task types

Il planner DEVE sempre emettere:
- task_type
- output_format

## task_type ammessi
- python_code
- shell_commands
- calculation
- structured_text
- generic_text

## output_format ammessi
- python
- shell
- plain_text
- json
- markdown

## Mapping
- script / python / plugin / programma -> python_code + python
- comandi shell / bash / terminale -> shell_commands + shell
- calcolo semplice -> calculation + plain_text
- json -> structured_text + json
- markdown -> structured_text + markdown
- altro -> generic_text + plain_text
