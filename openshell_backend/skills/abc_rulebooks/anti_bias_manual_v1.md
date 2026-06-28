# ABC Anti-Bias Manual v1

Scopo: impedire che il sistema trasformi desiderio, ansia o narrazione in numeri alti. Il calcolo resta deterministico; il manuale definisce vincoli interpretativi e limiti di peso.

## Regola 1: mind-reading non consentito

### Definizione

Mind-reading significa attribuire stati interni non osservati: "lei pensa che", "lei sente", "sta male per", "lo fa per", "vuole provocare", "mi controlla".

### Regola operativa

- Classificare sempre come `inference`, mai come `observed_fact`.
- Applicare tag `mind_reading`.
- Peso massimo assoluto: 30% del massimo peso di un fatto osservato.
- Applicare riduzione confidence aggiuntiva: `mind_reading_risk` = -0.05.

### Esempi

- "Arianna sta male per AntonLuca" -> `inference:mind_reading_non_supportato`.
- "Il tono non era allegro" -> `inference:tono_ambiguo`.
- "Mi guarda quando accedo" -> `inference:mind_reading_non_supportato`, non prova relazionale.

## Regola 2: micro-segnali vs macro-segnali

### Definizione

Un micro-segnale apre o mantiene il canale, ma non prova scelta, priorita o disponibilita romantica. Un macro-segnale implica costo, rinuncia, inclusione sociale o confine verso alternative.

### Micro-segnali

- Due chiacchiere.
- Battuta tipo "mi porti fuori".
- Messaggio leggero.
- Oggetto usato come aggancio.
- Logistica della bestia o commissione non emotiva.

### Limiti di peso

- `observed_fact:due_chiacchiere` <= +2.
- `observed_fact:messaggio_leggero` <= +3.
- `observed_fact:secure_base_pre_nodo` quando e solo casco/oggetto <= +3.
- `observed_fact:logistica_pura` <= +1.

### Macro-segnali

- Notte negata all'alternativa.
- Confine esplicito verso terzo.
- Inclusione sociale stabile.
- Investimento domestico ripetuto e non solo commissione.

## Regola 3: contraddizioni abbassano la confidence

### Definizione

Una contraddizione e un segnale che limita o contrasta una lettura positiva. Non serve a distruggere il campo, ma impedisce escalation dello score.

### Regola operativa

- Ogni `contradiction` riduce confidence di 0.1.
- Le contraddizioni non possono avere peso positivo.
- Se `confidence < 0.7`, le azioni ammesse sono solo `monitor_only` o `do_nothing_active`.
- Se esiste `contradiction_present`, il summary deve restare prudente.

### Esempi

- "Dice di voler festeggiare ma non fissa data" -> `contradiction:promessa_senza_data`.
- "Fabio vicino nel campo domestico ma escluso da uscita sociale" -> `contradiction:mancato_invito_sociale_esterno`.

## Regola 4: mancata inclusione sociale

### Definizione

Se Fabio non viene invitato a una scena sociale esterna mentre resta incluso in logistica o campo domestico, la posizione e ambivalente: prossimita si, default sociale no.

### Regola operativa

- Usare `contradiction:mancato_invito_sociale_esterno`.
- Peso: -7.
- Non interpretare come esclusione totale.
- Non permettere `light_open` se il punteggio prudenziale non supera 70 e confidence non supera 0.8.

## Regola 5: fame emotiva dell'osservatore

### Definizione

La fame emotiva e la spinta dell'analista/osservatore a cercare verifica immediata, conferma, chiarimento o prova romantica. Questa spinta puo gonfiare micro-segnali e sottopesare contraddizioni.

### Indicatori

- Fretta di chiedere chiarimenti.
- Lettura di ogni micro-contatto come prova.
- Necessita di sapere dove sia stata.
- Reazione ferita a mancato invito.
- Pressione su cinema, uscita, serata o definizione.

### Regola operativa

- Inserire flag o vincolo operativo quando il report contiene "non inseguire", "non chiedere", "non rilanciare".
- Non alterare lo score con questi vincoli; usarli per bloccare azioni forti.
- Se compare fame emotiva, preferire `do_nothing_active`.

## Regola 6: eventi passati non provano intenzione attuale

### Definizione

Un evento passato puo essere contesto, non prova di intenzione presente senza marker comportamentale fresco.

### Regola operativa

- Richiedere marker attuale: messaggio, chiamata, tempo dedicato, rinuncia ad alternativa, inclusione sociale.
- Se manca marker attuale, classificare come `inference` o `external_signal`.

## Regola 7: LLM escluso dallo scoring runtime

### Regola operativa

- Nessun LLM puo generare `raw_score`, `confidence`, `rlfull_current`, `prudential_score`, `action`.
- LLM ammesso solo in `review-rules` come proposta di modifica a manuali/pesi.
- Ogni proposta futura deve avere: diff, test di calibrazione, approvazione umana, nuovo version tag.

## Riferimenti

- Attachment e secure base: Bowlby/Ainsworth.
- Investment model: Rusbult.
- Triangles/differentiation: Bowen.
- Emotional response: LeDoux.
- Regolazione/pausa: DBT emotion regulation e distress tolerance.
