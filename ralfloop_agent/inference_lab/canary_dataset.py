from __future__ import annotations


def tokenizer_canaries() -> list[str]:
    bases = [
        "Ciao mondo",
        "Perché l'audio è fuori fase?",
        "Rispondi in italiano tecnico.",
        "Hello world",
        "don't assume; verify first",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        "for i in range(3): print(i)",
        '{"ok":true,"items":[1,2,3]}',
        '{\n  "name": "Ralf",\n  "enabled": false\n}',
        "àèìòù ÀÉ ñ ç 中文 日本語 العربية",
        "emoji 🚀🔒✅❌",
        ".,;:!?()[]{}<>+-=*/\\|_@#$%^&~`",
        "0 1 2 3 10 42 3.14159 -7 +99",
        "spazi   multipli\tTAB\nnewline\r\nCRLF",
        "<|im_start|>system\nTest<|im_end|>\n",
        "/chat/stream != /tasks/run",
        "path=/home/example/project/file.py",
        "SELECT id FROM tasks WHERE done = 0;",
        "curl -fsS http://127.0.0.1:19091/health",
        "class Café:\n    pass",
    ]
    prefixes = ("", "A: ", "  ", "\n", "id=42 ")
    return [prefix + base for prefix in prefixes for base in bases]
