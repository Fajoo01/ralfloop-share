role: coder

# Playbook task riusciti

## Shell temporaneo corretto
Input:
- dammi i comandi bash e validali davvero: usa mktemp per creare un file temporaneo, scrivici CIAO e leggilo

Output atteso:
FILE=$(mktemp)
echo "CIAO" > "$FILE"
cat "$FILE"

Regole:
- usare mktemp
- non usare /ralfloop_tmp per file temp shell generici
- niente spiegazioni nel candidate

## Calculation corretto
Input:
- risolvi l'equazione 2*x + 5 = 19

Output candidate buono:
x = (19 - 5) / 2
print(x)

Regole:
- usare Python breve
- stampare il risultato finale
- il final user-facing deve essere stdout normalizzato

## SymPy corretto
Input:
- calcola la derivata di x^3 + 2*x

Output candidate buono:
from sympy import symbols, diff

x = symbols('x')
print(diff(x**3 + 2*x, x))

## Sistema di equazioni corretto
Input:
- risolvi il sistema: x + y = 5, x - y = 1

Output candidate buono:
from sympy import symbols, Eq, solve

x, y = symbols('x y')
sol = solve((Eq(x + y, 5), Eq(x - y, 1)), (x, y))
print(f"x = {sol[x]}, y = {sol[y]}")
