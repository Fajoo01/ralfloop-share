# Paths e mounts reali

## Runtime path
Il path runtime per gli artifact temporanei è definito dal setting:
temp_output_dir

## Regola
Quando il task richiede di salvare file temporanei o artifact:
- usare il path indicato nel runtime context o nella configurazione
- non inventare path alternativi
- non usare tempfile.gettempdir()
- non usare os.environ["TEMP"]
- non usare path di sistema generici

## Host visibility
Il path concreto dipende dal mount attivo del container e dal setting corrente.
