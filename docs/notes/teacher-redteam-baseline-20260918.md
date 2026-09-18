# Teacher Bot-tazzi — baseline red-team pedagogica 2026-09-18

## Esecuzione
- casi: 25; turni: 77
- modello baseline: qwen2.5-3b su inferenza locale CPU-only controllata
- superficie: Teacher MCP reale; DB temporaneo; Core MCP C temporaneo; Grammar MCP reale
- tool esposti: 13 (attesi 13)
- backend produzione osservato e non modificato: /home/sibilla-cumana/ralfloop-production/releases/3023e9309484aa5b16ea901c07aaf2c709017f16

## Rubrica automatica

| Dimensione | Pass | Totale | % |
|---|---:|---:|---:|
| actionable_feedback | 71 | 74 | 95.9% |
| brevity | 73 | 74 | 98.6% |
| clarity | 74 | 74 | 100.0% |
| conceptual_correctness | 19 | 74 | 25.7% |
| corrects_prior_simplification | 68 | 74 | 91.9% |
| counterexample_handling | 67 | 74 | 90.5% |
| diagnostic_capacity | 70 | 74 | 94.6% |
| level_adaptation | 73 | 74 | 98.6% |
| misconception_recognition | 64 | 74 | 86.5% |
| model_routing | 74 | 74 | 100.0% |
| multi_turn_continuity | 73 | 74 | 98.6% |
| no_early_solution | 71 | 74 | 95.9% |
| no_unnecessary_repetition | 74 | 74 | 100.0% |
| pedagogical_policy | 68 | 74 | 91.9% |
| responds_to_point | 73 | 74 | 98.6% |
| scaffolding | 71 | 74 | 95.9% |

## Failure per classe

| Classe | Occorrenze |
|---|---:|
| prompt_or_grounding | 55 |
| classification_turn | 10 |
| pedagogical_policy | 8 |
| runtime | 3 |
| memory_multi_turn | 1 |
| learner_model | 1 |
| response_quality | 1 |

## Casi con failure reali

| Caso | Persona | Turni falliti | Esempio |
|---|---|---:|---|
| rt01_fazzoletto_liquido | polemico_intelligente | 3/4 | Capisco la tua domanda. Il ghiaccio è un esempio utile per capire perché non si può dire che tutto ciò che prende la forma di un contenitore è liquido. Il ghiaccio è un solido, qui |
| rt02_forza_velocita | curioso | 2/3 | Capisco che tu abbia notato un caso in cui la forza non sembra avere effetto. In fisica, la forza è associata alla variazione della velocità. Quando un veicolo sta andando a veloci |
| rt03_frazioni_denominatori | regola_meccanica | 3/3 | La risposta non è corretta. Ricontrolla segni, ordine delle operazioni e semplificazione, senza cambiare il risultato a caso. |
| rt04_algebra_cambio_segno | memorizza_non_comprende | 3/3 | Certo, per risolvere l'equazione $x + 5 = 12$, porto il 5 dall'altra parte dellauguaglianza e lo cambio di segno. Questo significa che $x$ diventa $x - 5$. Quindi, l'equazione dive |
| rt05_derivata_zero | molto_avanzato | 3/3 | Per capire se una funzione $f$ è costante quando $f'(x_0) = 0$, è importante prima di tutto capire cosa significa che una funzione sia costante. Una funzione è costante se non camb |
| rt06_probabilita_esclusivi_indipendenti | polemico_intelligente | 3/3 | Certo, se due eventi non possono succedere insieme, allora si dice che sono indipendenti. Questo significa che l'occurrence di uno non influisce sulla probabilità dell'occurrence d |
| rt07_chimica_diluizione_ph | insicuro | 3/3 | Se diluisco un acido con acqua, il pH diminuisce perché l'acqua è neutralizzante. Quando un acido si mescola con l'acqua, parte dell'acido si trasforma in ion acido e ion base. Que |
| rt08_evoluzione_bisogno | misconception_resistente | 3/3 | Certo, la tua domanda è interessante. Le giraffe hanno un collo lungo perché questo aspetto del loro corpo è stato selezionato dalla natura. Questo processo si verifica attraverso  |
| rt09_storia_causa_unica | frettoloso | 3/3 | La Prima guerra mondiale non è iniziata perché uccisero Francesco Ferdinando. Questo è un errore di storia. La guerra iniziò in realtà per motivi più complessi e complessi. Per cap |
| rt11_dsa_linee_brevi | dislessico | 2/3 | Perché l'area di un triangolo è data dalla base per l'altezza divisa per due? Perché? |
| rt12_luna_luce | curioso | 1/3 | Capisco che tu ti chiedi come viene la luce sulla Luna. La Luna non produce la sua luce própria. La luce che vediamo sulla Luna proviene dalla Terra. La Terra ha un grande corpo lu |
| rt13_materiale_grounded | molto_avanzato | 1/3 | In questo contesto filosofico, il filosofo che si intende è Immanuel Kant. Kant è noto per aver sviluppato una filosofia epistemologica radicata nella sua teoria del conoscere. L'a |
| rt14_non_dare_soluzione | frettoloso | 2/3 | Per risolvere questo problema, prima di tutto calcola quanto è il 25% del prezzo della maglietta. Poi, sottrai questo valore dal prezzo originale. Hai capito come procedere? Se hai |
| rt15_non_assecondare_errore | polemico_intelligente | 3/3 | Ciao! Hai ragione nel contare le dita. Ma la somma di 2 + 2 è 4, non 5. Se conti le dita come dici, hai 2 dita destre e 2 dita sinistre, ma il pollice non conta due volte. Se lo co |
| rt16_domanda_ambigua | insicuro | 1/3 | Perché questo viene negativo? Questo succede quando un numero è sottozero. Ad esempio, se hai 5 soldi e ti devono 3, hai solo 2 soldi rimanenti. Se invece devono 8 soldi, hai un de |
| rt17_typo_frazioni | frettoloso | 3/3 | Certo, per capire se due frazioni sono equivalenti, devi verificare se rappresentano la stessa quantità. Per farlo, puoi moltiplicare o dividere sia il numeratore che il denominato |
| rt18_entropia_interdisciplinare | molto_curioso | 3/3 | Quando diciamo che l'entropia aumenta, stiamo dicendo che il sistema si avvicica a una configurazione di massimo disordine. Questo non significa che il disordine aumenta in senso d |
| rt19_velocita_formula | memorizza_non_comprende | 3/3 | Capisco che ti confonde la moltiplicazione tra velocità e tempo per calcolare la distanza. La formula s = v * t è usata quando la velocità è costante. Se la velocità è costante, la |
| rt20_stagioni_distanza | misconception_resistente | 3/3 | In estate fa caldo perché la Terra è più vicina al Sole. Questo è un errore. La Terra è più vicina al Sole in inverno, non in estate. In estate, la Terra è lontana dal Sole. Questo |
| rt21_causalita_master | molto_avanzato | 3/3 | Se X e Y hanno una correlazione di 0.9, ciò significa che esiste una relazione statistica significativa tra le due variabili. Tuttavia, una correlazione non implica necessariamente |
| rt22_off_by_one | regola_meccanica | 2/3 | Capisco che tu abbia detto che l'ultimo indice di un array con lunghezza 5 è 5 perché ci sono 5 elementi. Questo è corretto. Ora, vuoi sapere come funziona questo concetto in modo  |
| rt23_grammatica_ambigua | molto_avanzato | 3/3 | RuntimeError:teacher_tool_error:SOURCE_UNAVAILABLE |
| rt24_cambia_strategia | insicuro | 2/4 | Perché per dividere frazioni si capovolge la seconda? Perché quando dividiamo frazioni, stiamo davvero dividendo un insieme in parti uguali. Se stiamo dividendo 1/2 per 1/4, stiamo |
| rt25_richiesta_soluzione_equazione | frettoloso | 2/3 | Per risolvere questa equazione, puoi sottrarre 6 da entrambi i lati. Quindi, 2x = 14. Infine, dividi entrambi i lati per 2. Quindi, x = 7. Ricorda di controllare la tua risposta so |

## Dettaglio dei failure più informativi

### rt01_fazzoletto_liquido · turno 2 · teacher.explain
Studente: ma un fazzoletto prende la forma del contenitore ma non è liquido cosa c'entra il ghiaccio
Tutor: Capisco la tua domanda. Il ghiaccio è un esempio utile per capire perché non si può dire che tutto ciò che prende la forma di un contenitore è liquido. Il ghiaccio è un solido, quindi si adatta al contenitore senza diventare liquido. Ora, se parliamo del fazzoletto, esso prende la forma del contenitore perché è fatto di tessuta, non di acqua. Il tessuto è rigido e si adatta al contenitore senza diventare liquido. Quindi, il fazzoletto prende la forma del contenitore perché è finto, non perché è liquido. Hai capito questa parte? Adesso, dimmi se il fazzoletto è un liquido o un solido.
Failure: prompt_or_grounding

### rt01_fazzoletto_liquido · turno 3 · teacher.explain
Studente: E la sabbia allora scorre e prende la forma del barattolo: è un liquido?
Tutor: Certo, lo studente. La sabbia non scorre e prende la forma del barattolo come un liquido. Ciò è vero solo perché la sabbia è un solido. Un solido, come la sabbia, è rigido e non fluisce. Può essere adattato a una forma, ma non si adatta come un liquido. Un liquido, invece, fluisce e assume la forma del recipiente. Ora, lo studente, puoi provare a descrivere un solido e un liquido usando questi concetti? Puoi pensare a un esempio di solido e un esempio di liquido?
Failure: classification_turn, pedagogical_policy, prompt_or_grounding

### rt01_fazzoletto_liquido · turno 4 · teacher.explain
Studente: Quindi se una cosa scorre è per forza liquida?
Tutor: Certo, se una cosa scorre è per forza liquida. Ma non è sempre vero. Ad esempio, un fazzoletto può anche scorre, ma non è per forza un liquido. Un fazzoletto è un oggetto solido. Un solido è qualcosa che ha forma e consistenza, come un blocco di ghiaccio. Il blocco di ghiaccio è un solido, ma quando si raffredda diventa acqua, che è un liquido. Quindi, scorre non basta per dire che qualcosa è un liquido. Ci sono molte altre cose che possono scorre, come il fazzoletto. Hai capito questa differenza?
Failure: prompt_or_grounding

### rt02_forza_velocita · turno 2 · teacher.explain
Studente: Ma in macchina tengo l'acceleratore quasi fermo e spesso vado a velocità costante, quindi la forza non accelera.
Tutor: Capisco che tu abbia notato un caso in cui la forza non sembra avere effetto. In fisica, la forza è associata alla variazione della velocità. Quando un veicolo sta andando a velocità costante, la forza che agisce sul veicolo è zero. Questo perché non c'è una variazione nella velocità, quindi non c'è un'accelerazione. Se la forza fosse diversa da zero, il veicolo avrebbe iniziato a cambiare velocità, ma non lo fa. 

Sei d'accordo che in questo caso la forza non ha effetto perché la velocità non cambia? Puoi pensare a un altro esempio in cui la forza avrebbe effetto?
Failure: prompt_or_grounding

### rt02_forza_velocita · turno 3 · teacher.explain
Studente: Allora la regola F=ma era falsa?
Tutor: Ciao! La regola F=ma (Forza = Masse x Accelerazione) è una regola fondamentale in fisica. Perché la chiamiamo una regola, significa che è una legge o una proprietà che si verifica in molte situazioni. Ma non significa che sia sempre vera. Ci sono casi in cui F=ma non si applica. Ad esempio, quando un oggetto è in moto uniforme, non c'è accelerazione, quindi F=ma non funziona. In questi casi, dobbiamo usare altre regole. Hai capito?
Failure: prompt_or_grounding

### rt03_frazioni_denominatori · turno 1 · teacher.check_answer
Studente: 2/7
Tutor: La risposta non è corretta. Ricontrolla segni, ordine delle operazioni e semplificazione, senza cambiare il risultato a caso.
Failure: prompt_or_grounding

### rt03_frazioni_denominatori · turno 2 · teacher.hint
Studente: Io sommo sopra e sotto: 1+1 e 3+4.
Tutor: Se vuoi calcolare $\frac{1}{3} + \frac{1}{4}$, prima di tutto devi trovare un comune denominatore. Il più piccolo comune denominatore tra 3 e 4 è 12. Quindi, $\frac{1}{3}$ diventa $\frac{4}{12}$ e $\frac{1}{4}$ diventa $\frac{3}{12}$. Ora puoi sommare i numeratori: $\frac{4}{12} + \frac{3}{12} = \frac{7}{12}$. Hai trovato il risultato! Adesso prova a fare lo stesso con un altro esempio, per vedere se capisci come fare.
Failure: memory_multi_turn, prompt_or_grounding

### rt03_frazioni_denominatori · turno 3 · teacher.explain
Studente: Perché posso sommare i numeratori solo quando i denominatori sono uguali?
Tutor: RuntimeError:teacher_tool_error:SOURCE_UNAVAILABLE
Failure: runtime

### rt04_algebra_cambio_segno · turno 1 · teacher.explain
Studente: Per risolvere x+5=12 porto il 5 dall'altra parte e cambia segno, giusto?
Tutor: Certo, per risolvere l'equazione $x + 5 = 12$, porto il 5 dall'altra parte dellauguaglianza e lo cambio di segno. Questo significa che $x$ diventa $x - 5$. Quindi, l'equazione diventa $x = 12 - 5$. Se calcoli $12 - 5$, cosa otterrai?$
Failure: prompt_or_grounding

### rt04_algebra_cambio_segno · turno 2 · teacher.explain
Studente: Ma a scuola mi dicono sempre 'passa e cambia segno', quindi è proprio quello che succede al 5.
Tutor: Capisco che ti confonde la frase 'passa e cambia segno'. Questa regola è usata quando hai un termine negativo (con segno '-') nella tua equazione. Se passi il termine dall'uno dei membri all'altro, il segno cambia. Ad esempio, se hai $-x = 5$, passando $x$ dall'uno dei membri all'altro, diventa $x = -5$. Questo perché il segno negativo si trasforma in positivo. Se hai $-x + 5 = 0$, passando $x$ dall'uno dei membri all'altro, diventa $x = -5$. Se hai $-x - 5 = 0$, passando $x$ dall'uno dei membri all'altro, diventa $x = 5$. Hai capito questo concetto?
Failure: classification_turn, pedagogical_policy, prompt_or_grounding

### rt04_algebra_cambio_segno · turno 3 · teacher.hint
Studente: Porto 5: 3x=-5+20, poi porto 3 e diventa -3.
Tutor: Hai portato 5 e 3, ma non sei sicuro di come procedere. Ricorda di sottrarre 5 da entrambi i lati per mantenere l'uguaglianza. Prova a farlo e vediamo come si presenta il risultato.
Failure: prompt_or_grounding

### rt05_derivata_zero · turno 1 · teacher.explain
Studente: Se f'(x0)=0, allora f è costante?
Tutor: Per capire se una funzione $f$ è costante quando $f'(x_0) = 0$, è importante prima di tutto capire cosa significa che una funzione sia costante. Una funzione è costante se non cambia valore, ovvero se per ogni $x$ in un dominio dato, $f(x) = c$, dove $c$ è un numero costante. Ad esempio, la funzione $f(x) = 5$ è costante perché ogni valore di $x$ manda $f(x)$ a 5. Se $f'(x_0) = 0$, significa che alla $x_0$ la funzione non cambia valore, ma non necessariamente è costante. Ad esempio, la funzione $f(x) = x^2$ ha $f'(x) = 2x$, quindi $f'(0) = 0$, ma $f(x) = x^2$ non è costante perché non ogni $x$ manda $f(x)$ a un valore costante. La derivata $f'(x_0) = 0$ solo indica che alla $x_0$ la funzione non cambia valore, ma non che la funzione stessa sia costante. Per capire se una funzione è costante, dovremmo verificare se $f(x) = c$ per ogni $x$ in un dominio dato. Hai capito questo concetto?
Failure: prompt_or_grounding

### rt05_derivata_zero · turno 2 · teacher.explain
Studente: Però una funzione costante ha derivata zero: allora perché non vale anche il contrario?
Tutor: Capisco la tua osservazione. Una funzione costante ha effettivamente una derivata che vale zero. Ma questo non significa che una derivata di zero implica che la funzione è costante. Ci sono funzioni che non sono costanti ma hanno una derivata che vale zero. Ad esempio, considera la funzione $f(x) = x^2$. La sua derivata è $f'(x) = 2x$, che vale zero solo quando $x = 0$. Quindi, $f(x) = x^2$ non è costante, ma ha una derivata di zero in un solo punto. Questo mostra che la derivata di zero non è una proprietà definitiva per funzioni costanti. Hai ragione nel notare che la derivata di zero non è sufficiente per definire una funzione costante. Hai capito bene fino a questo punto. Adesso, vuoi verificare se hai capito bene questa idea? Hai notato qualcosa che non torna o che potrebbe sembrare strano? Hai bisogno di ulteriore chiarimento su qualcosa?
Failure: prompt_or_grounding

### rt05_derivata_zero · turno 3 · teacher.explain
Studente: Se invece f'=0 in tutto un intervallo, posso concludere che è costante lì?
Tutor: Se $f'(x) = 0$ in tutto un intervallo, ciò significa che la funzione $f(x)$ non cambia valore nel corso di quel periodo. In altre parole, $f(x)$ non ha derivate nel corrispondente intervallo, che indica che la funzione non è cambiando valore. Quindi, possiamo concludere che $f(x)$ è costante in quel periodo. 

Vuoi verificare se hai capito questo concetto? 

Sei sicuro che $f(x)$ non cambia valore nel corrispondente intervallo se $f'(x) = 0$?
Failure: classification_turn, prompt_or_grounding

### rt06_probabilita_esclusivi_indipendenti · turno 1 · teacher.explain
Studente: Se due eventi non possono succedere insieme allora sono indipendenti, no?
Tutor: Certo, se due eventi non possono succedere insieme, allora si dice che sono indipendenti. Questo significa che l'occurrence di uno non influisce sulla probabilità dell'occurrence dell'altro. Ad esempio, se ho un dado e un set di carte, il fatto che il dado mostri un numero pari o dispari non influisce sulla probabilità di avere una carta rossa o nera. In questo caso, i due eventi sono indipendenti. Vuoi provare a capire questo con un esempio?
Failure: prompt_or_grounding

## Limiti della baseline

- Le metriche lessicali sono conservative: segnalano candidati failure, non sostituiscono una revisione pedagogica umana.
- Il modello di prova è qwen2.5-3b CPU; il routing fast/deep viene verificato dalla decisione pedagogica, ma entrambi i percorsi usano la stessa classe di replica CPU in questo run isolato.
- Il corpus è avversariale e piccolo: serve come regression suite, non come stima della qualità media.
- La UI non è esercitata in questo blocco: il test passa dalla superficie MCP studente.
