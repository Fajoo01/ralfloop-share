role: judge

# Checklist validazione per task

## Shell
- usa mktemp o /tmp
- niente markdown nel candidate
- comandi eseguibili
- stdout coerente

## Python
- codice eseguibile
- import coerenti
- niente prosa
- niente placeholder inutili

## Calculation
- usare stdout come fonte finale
- se stdout è 7.0 e rappresenta intero, final = 7
- niente spiegazioni se non richieste

## SymPy
- preferire sympy per equazioni, derivate, integrali, sistemi
- risultato finale leggibile
- candidate minimalista

## Host-visible
- NON baseline
- trattare separatamente
- non promuovere patch host-visible senza smoke test completo
