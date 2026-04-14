# Manuale base di grammatica italiana

## Obiettivo
Fare analisi grammaticale token per token.

## Categorie ammesse
- articolo_determinativo
- articolo_indeterminativo
- nome_comune
- nome_proprio
- aggettivo_qualificativo
- pronome
- preposizione
- preposizione_articolata
- congiunzione
- avverbio
- verbo
- interiezione
- numerale
- segno_punteggiatura

## Regole minime
- Ogni token della frase deve comparire una sola volta nell'output.
- L'output deve essere un array JSON.
- Ogni elemento deve avere:
  - token
  - categoria
- La categoria deve essere una delle categorie ammesse.
- Non aggiungere spiegazioni fuori dal JSON.
- Mantieni i token nello stesso ordine della frase originale.

## Esempio
Frase: Il gatto nero corre veloce.
Output:
[
  {"token":"Il","categoria":"articolo_determinativo"},
  {"token":"gatto","categoria":"nome_comune"},
  {"token":"nero","categoria":"aggettivo_qualificativo"},
  {"token":"corre","categoria":"verbo"},
  {"token":"veloce","categoria":"avverbio"},
  {"token":".","categoria":"segno_punteggiatura"}
]
