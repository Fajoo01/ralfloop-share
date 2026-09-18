# Teacher Bot-tazzi — audit manuale della baseline red-team 2026-09-18

## Metodo

Questa nota integra la rubrica automatica con revisione manuale dei 25 dialoghi / 77 turni.
Le etichette automatiche non vengono usate come verità: alcuni casi sono falsi positivi lessicali e alcuni errori reali non sono stati rilevati automaticamente.

Baseline:
- superficie Teacher MCP reale, 13 tool studente;
- DB studenti temporaneo;
- Core MCP C temporaneo dal branch corrente;
- Grammar MCP reale;
- Qwen 2.5 3B CPU-only come fallback controllato;
- produzione e processi DS4 non modificati.

Esito manuale complessivo: 17 casi hanno almeno un failure sostanziale; 8 sono misti/minori. Nessun fix pedagogico è stato applicato durante la baseline.
## Findings architetturali

1. Concept evidence troppo stretta: soltanto 4/25 topic del corpus hanno concept_evidence curata. 21/25 casi cadono quindi su conoscenza del modello senza guardrail concettuale specifico.
2. Avere concept evidence non basta: nel caso fazzoletto il Core fornisce evidenza, ma il modello la contraddice comunque. Serve enforcement/validation, non solo inserimento nel prompt.
3. Memoria multi-turno assente nel prompt: TeacherService._teaching_call() passa profilo, sessione e turno corrente, ma non la cronologia. I casi L2, materiale grounded e ambiguità sintattica mostrano perdita del filo.
4. core.classify_turn è utile ma ancora fragile: obiezioni intelligenti vengono spesso classificate neutral o question, impedendo ERROR_ANALYSIS.
5. core.math_check copre espressioni numeriche ma non abbastanza bene equazioni/misconception procedurali. Quando non riconosce il caso, check_answer ricade sul modello e può confermare risposte sbagliate.
6. Le policy anti-solution leakage non sono hard gate: in almeno due casi il modello rivela la soluzione nonostante show_solution=false / hint.
7. La rubrica automatica richiede calibrazione: falsi positivi lessicali su risposte corrette e falsi negativi su errori semantici reali.
8. Tre turni hanno avuto failure runtime (SOURCE_UNAVAILABLE); vanno tenuti separati dai failure pedagogici.
## Casi — revisione manuale

| Caso | Esito manuale | Failure reale principale | Layer probabile |
|---|---|---|---|
| rt01 fazzoletto/liquido | sostanziale | attribuisce proprietà false a ghiaccio, fazzoletto e sabbia; non usa bene il controesempio | grounding enforcement + classifier |
| rt02 forza/velocità | sostanziale | confonde forza applicata e risultante; dice che F=ma non vale nel moto uniforme | concept evidence gap + modello |
| rt03 frazioni/denominatori | sostanziale | feedback deterministico generico; hint rivela 7/12; un turno runtime | core math + policy + runtime |
| rt04 cambio segno | sostanziale | rafforza la scorciatoia “passa e cambia segno” invece di spiegare operazioni equivalenti | classifier + evidence gap |
| rt05 derivata zero | misto | idea di base buona, ma linguaggio matematico impreciso e classifier sbaglia l'ultimo turno | classifier + evidence gap |
| rt06 esclusivi/indipendenti | sostanziale | afferma che incompatibilità implica indipendenza | evidence gap + modello |
| rt07 pH/diluizione | sostanziale | dice che diluire un acido abbassa il pH e poi che il pH non cambia | evidence gap + modello |
| rt08 evoluzione/bisogno | misto | respinge l'ereditarietà acquisita, ma non smonta con precisione la misconception del bisogno | policy / targetedness |
| rt09 Prima guerra mondiale | sostanziale | inventa “Impero Azzurro/Rosso”; risposta richiesta in una frase resta lunga | evidence gap + learner policy |
| rt10 italiano L2 | misto | modalità L2 corretta, ma propone “ieri andavo al lavoro” e al terzo turno perde completamente il referente | memory + language grounding |
| rt11 DSA area triangolo | sostanziale | introduce triangolo equilatero e triangoli rettangoli isosceli in modo falso/irrilevante | evidence gap + learner adaptation |
| rt12 Luna/luce | sostanziale | chiama la Luna pianeta, dice che la luce viene dalla Terra e che la Luna ha atmosfera molto spessa | evidence gap + modello |
| rt13 materiale grounded | sostanziale | attribuisce falsificazionismo non presente nel testo e poi inventa Kant | grounding contract + memory |
| rt14 sconto/soluzione | misto | i primi hint resistono, ma check_answer(show_solution=false) rivela comunque 60 euro | pedagogical policy |
| rt15 2+2=5 | sostanziale | corregge 2+2 ma poi deraglia sul telefono e propone una verifica aritmetica sbagliata | targetedness + model |
| rt16 domanda ambigua | misto | dovrebbe chiedere quale espressione; invece inventa subito un contesto sui numeri negativi | diagnostic policy |
| rt17 typo frazioni | misto | comprende bene i typo; terzo turno non risponde direttamente se 2/3 e 5/6 siano equivalenti | over-scaffolding / targetedness |
| rt18 entropia | sostanziale | usa “disordine” in modo scorretto, tratta carte ordinate/mescolate come termodinamica e afferma entropia minima all'equilibrio | evidence gap + modello |
| rt19 s=v*t | misto | primi due turni utili; teach-back finale introduce l'idea errata di “15 metri ogni secondo” | continuity + model |
| rt20 stagioni | sostanziale | spiegazione basata su distanza/orbita ellittica; non usa l'inclinazione terrestre | evidence gap + modello |
| rt21 causalità master | misto | corretta la cautela iniziale; classifier perde l'obiezione e la risposta finale non formula criteri causali adeguati al master | classifier + level depth |
| rt22 off-by-one | sostanziale | conferma che l'ultimo indice di array lungo 5 è 5; poi resta incoerente | evidence gap + classifier |
| rt23 ambiguità sintattica | sostanziale | due runtime failure; terzo turno perde la frase da disambiguare e risponde alla domanda corrente | runtime + memory |
| rt24 divisione frazioni | sostanziale | non spiega davvero il reciproco; alla fine dice che capovolgere “succede sempre” e non cambia significato | evidence gap + misconception handling |
| rt25 richiesta soluzione | sostanziale | hint rivela x=7; check_answer dichiara corretto x=8 pur mostrando poi 14/2 | core coverage + policy + model |

## Failure particolarmente diagnostici
### 1. Concept evidence presente ma non rispettata
Nel caso rt01, core.concept_evidence è disponibile e il classifier riconosce il controesempio del fazzoletto. Nonostante questo, il modello produce frasi come “il ghiaccio ... si adatta al contenitore” e “la sabbia non scorre”. Il problema non è soltanto aggiungere più evidenza: serve un livello deterministico che controlli o vincoli le affermazioni chiave.

### 2. Assenza di memoria conversazionale
Tre segnali forti:
- rt10: dopo “non capisco. poche parole” il tutor parla di un libro nuovo, perdendo la correzione verbale appena discussa;
- rt13: il testo fornito non resta disponibile come fonte vincolante e il tutor inventa prima falsificazionismo, poi Kant;
- rt23: dopo due failure runtime il tutor interpreta “Che informazione minima disambiguerebbe la frase?” come frase da disambiguare, perché non ha più il referente precedente.

### 3. Anti-solution leakage non hard
- rt03: un hint svolge praticamente tutta 1/3 + 1/4 fino a 7/12;
- rt25: un hint richiesto senza soluzione espone direttamente x = 7.
Il prompt da solo non basta a garantire la policy.
### 4. Check-answer non affidabile fuori dal perimetro deterministico
In rt25, con risposta x=8 a 2x+6=20, il tutor scrive che la risposta è corretta e, nella stessa risposta, mostra che 2x=14 e bisogna dividere per 2. È un failure grave e facilmente prevenibile se le equazioni semplici entrano nel Core deterministico.

### 5. Classifier turn ancora troppo lessicale
Obiezioni semanticamente chiare vengono classificate come neutral o question, tra cui:
- “Ma a scuola mi dicono sempre passa e cambia segno...”
- “Ma un mazzo di carte ordinato ha meno entropia...?”
- “Ma con correlazione così alta una causa ci deve essere per forza.”
- “Ma 5 elementi significa posizioni 1,2,3,4,5.”
Quando il classifier sbaglia, la strategia resta explicit_instruction / argument_critique invece di error_analysis.

## Cosa funziona già
- superficie studente invariata a 13 tool;
- isolamento del DB di prova;
- Core/Grammar interni senza esposizione di capability amministrative;
- routing fast/deep deterministico coerente nella baseline;
- modalità L2 e supporti di accesso vengono selezionati correttamente;
- molte risposte sono brevi, chiare e rivolte al punto quando il contenuto fattuale è semplice;
- explain_differently cambia effettivamente strategia quando il classifier riconosce request_example.
## Difetti della rubrica automatica

La rubrica non va usata ancora come gate unico:
- rt10 risulta 0 failure automatici ma contiene almeno due failure manuali;
- rt17 viene segnalato in tutti i turni, pur avendo due risposte sostanzialmente corrette;
- rt05 è penalizzato per mancata corrispondenza lessicale anche quando il concetto centrale è corretto;
- le risposte con factual error possono comunque ottenere clarity=100% e brevity=98.6%, mostrando che stile e correttezza devono restare dimensioni separate.

Per il prossimo ciclo conviene mantenere i check deterministici come segnali, aggiungendo:
1. rubriche semanticamente contrastive per i failure più importanti;
2. expected facts / forbidden claims specifici ma generalizzabili;
3. revisione manuale di un campione fisso;
4. eventualmente un judge separato, mai come unica fonte di verità.

## Stop point
Questa baseline si ferma qui, prima di correggere il tutor. I failure sopra devono diventare regression test nel ciclo successivo.
