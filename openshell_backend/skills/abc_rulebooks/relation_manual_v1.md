# ABC Relation Manual v1

Scopo: trasformare concetti relazionali in evidenze operative osservabili. Il manuale non calcola numeri: i numeri sono solo in `evidence_weights_v1.json` e la formula e solo in `abc_formula_loop.py`.

## Uso operativo

- Una evidenza vale solo se ha un comportamento osservabile o una frase testuale.
- Una deduzione psicologica resta `inference`, anche se plausibile.
- Un segnale positivo non cancella un limite sociale nello stesso episodio.
- Un segnale negativo non cancella automaticamente un contatto positivo.
- La lettura finale deve restare prudente quando il campo contiene micro-aperture e limiti sociali insieme.

## Teoria dell'attaccamento: Bowlby e Ainsworth

### Definizione

L'attaccamento descrive il modo in cui una persona cerca sicurezza, conforto e base di esplorazione tramite una figura significativa. In termini operativi:

- `safe haven`: la persona cerca l'altro dopo stress, paura, fatica, conflitto o bisogno di regolazione.
- `secure base`: la persona usa l'altro come appoggio prima di affrontare un nodo, una scelta o un compito.

Il passaggio da solo `safe haven` a anche `secure base` e piu forte: non indica solo scarico emotivo, ma fiducia anticipatoria.

### Indicatori comportamentali

- Contatto cercato dopo evento stressante: chiamata dopo fatica, rientro, litigio, nodo familiare.
- Contatto o oggetto prima di un nodo: casco, chiavi, commissione, appoggio pratico prima di lavoro o uscita.
- Ricerca spontanea senza necessita tecnica: messaggio, chiamata, passaggio, proposta di parlare.
- Ritorno alla conversazione dopo una pausa o dopo un evento carico.

### Peso relativo

- Alto: `notte_negata_alternativa`, confine netto verso terzo, contatto pre-nodo forte e ripetuto.
- Medio: `secure_base_pre_nodo`, `contatto_spontaneo`, `rientro_immediato_post_nodo`.
- Basso: oggetto singolo ambiguo, battuta, micro-contatto senza continuita.

### Collegamento evidenze

- `observed_fact:secure_base_pre_nodo`: oggetto o appoggio prima di un nodo. Nel caso del casco il peso e basso perche il gesto e concreto ma ancora leggero.
- `observed_fact:rientro_immediato_post_nodo`: ritorno o contatto dopo nodo.
- `observed_fact:contatto_spontaneo`: chiamata o messaggio non imposto dalla situazione.
- `observed_fact:due_chiacchiere`: micro-apertura, peso basso se non e seguita da inclusione sociale.

## Teoria dell'investimento: Rusbult

### Definizione

Il modello dell'investimento valuta stabilita/commitment tramite tre assi:

- soddisfazione della relazione o interazione;
- qualita delle alternative disponibili;
- investimento gia accumulato nella relazione.

Un gesto singolo conta poco. Conta di piu quando mostra costo, rinuncia, continuita o sostituzione reale di alternative.

### Indicatori comportamentali

- Alternativa ridotta: notte negata, opzione concorrente accorciata, terzo meno centrale.
- Investimento: tempo dedicato, routine, oggetti, commissioni condivise, cura domestica ripetuta.
- Soddisfazione: contatto cercato senza pressione, conversazione prolungata, tono caldo verificabile.

### Peso relativo

- Alto: `notte_negata_alternativa` (+15) perche segnala qualita dell'alternativa in calo.
- Medio: investimento domestico/routine condivisa (+6/+10 secondo versione e contesto).
- Basso: logistica pura (+1), battuta, messaggio leggero, singolo aggancio.

### Collegamento evidenze

- `observed_fact:notte_negata_alternativa`: alto.
- `observed_fact:investimento_familiare`: medio se c'e routine o gestione condivisa, non semplice commissione.
- `observed_fact:logistica_pura`: basso; bestia/commissione senza marker emotivo autonomo.
- `external_signal:campo_quotidiano`: contesto, non prova di scelta.

## Triangolazione e differenziazione: Bowen

### Definizione

Un triangolo emotivo si forma quando la tensione tra due persone viene stabilizzata tramite un terzo. La differenziazione aumenta quando la diade diventa piu chiara e i confini verso il terzo sono piu netti.

### Indicatori comportamentali

- Triangolazione: opacita selettiva, terzo nominato ma non chiarito, uscita sociale senza Fabio dopo micro-contatto.
- Differenziazione: confini espliciti verso il terzo, notte negata, inclusione sociale chiara, meno ambiguita.
- Diade piu stabile: Fabio viene incluso non solo in logistica domestica ma anche in scelta sociale.

### Peso relativo

- Alto positivo: confini notturni o alternativi netti (+15), riduzione opacita con fatto osservato (+10).
- Medio positivo: marking territoriale verificabile (+5/+7).
- Negativo: mancata inclusione sociale esterna (-7), opacita terzo (-6).

### Collegamento evidenze

- `contradiction:mancato_invito_sociale_esterno`: Fabio vicino nel campo domestico ma escluso da scena sociale.
- `contradiction:opacita_terzo`: il terzo resta non chiarito.
- `observed_fact:marking_territoriale`: marker pubblico/default, valido solo se non negato da "non ancora incluso".

## Regolazione emotiva e processi decisionali

### Definizione

Un episodio emotivamente carico puo attivare risposte rapide e difensive. LeDoux colloca l'amigdala nel circuito che collega stimoli esterni a risposte di difesa. In DBT, le abilita di regolazione/distress tolerance servono a interrompere risposte impulsive e creare una pausa.

### Indicatori comportamentali

- Pausa dopo evento carico.
- Ricerca di attivita sociale neutra.
- Tono scarico o non allegro dopo giornata lunga.
- Evitamento di chiarimenti immediati.

### Peso relativo

- La pausa regolativa non e prova di freddezza.
- Il tono percepito e `inference`, non `observed_fact`.
- La ricerca di neutralita sociale puo essere regolazione, non disinteresse.

### Collegamento evidenze

- `inference:tono_ambiguo`: peso basso/negativo, per rischio mind-reading.
- `operational_constraint:non_inseguire`: non altera lo score, protegge l'azione.
- `contradiction:silenzio_diretto_su_nodo`: riduce confidence se il nodo resta evitato.

## Marking territoriale

### Definizione

Il marking territoriale e un segnale di presenza nello spazio dell'altro: oggetti, cibo, commissioni, routine, riferimenti pubblici o default condivisi.

### Indicatori comportamentali

- Oggetto lasciato o affidato.
- Cibo o cura pratica condivisa.
- Commissione gestita come routine comune.
- Inclusione in spazio domestico o familiare.

### Peso relativo

- Medio se ripetuto e non solo logistico.
- Basso se singolo, ambiguo o agganciato a necessita pratica.
- Cumulativo solo se le evidenze sono indipendenti.

### Collegamento evidenze

- `observed_fact:marking_territoriale`: medio.
- `observed_fact:secure_base_pre_nodo`: basso/medio secondo intensita.
- `observed_fact:logistica_pura`: basso, massimo +1.

## Riferimenti teorici

- Bowlby, J.; Ainsworth, M. D. S.: secure base, attachment behavior, strange situation.
- Rusbult, C. E.: investment model, commitment, satisfaction, alternatives, investment.
- Bowen family systems theory: triangles and differentiation of self.
- LeDoux, J. E.: amygdala and defensive emotional response.
- Linehan/DBT: emotion regulation, distress tolerance, STOP/pause before impulsive action.

Fonti web consultate per questa versione:

- https://pmc.ncbi.nlm.nih.gov/articles/PMC4085672/
- https://psychology.psy.sunysb.edu/attachment/online/inge_origins%20DP1992.pdf
- https://faculty.wcas.northwestern.edu/eli-finkel/documents/6_Rusbult1980_JournalOfExperimentalSocialPsychology.pdf
- https://docs.lib.purdue.edu/context/psychpubs/article/1025/viewcontent/Investment_Model_of_Commitment_Processes_Author_Accepted_Manuscript_Agnew.pdf
- https://www.thebowencenter.org/triangles
- https://pubmed.ncbi.nlm.nih.gov/14514027/
- https://pmc.ncbi.nlm.nih.gov/articles/PMC5021701/
