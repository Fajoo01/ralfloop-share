#!/usr/bin/env python3
"""Loopback-only default. Production publication requires explicit origin and HTTPS."""
import argparse
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ralfloop_agent.teacher.web.api import create_app
from ralfloop_agent.teacher.web.application import LearningApplication
from ralfloop_agent.teacher.web.client import DemoTeacher, TeacherClient
from ralfloop_agent.teacher.web.state import State


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("TEACHER_WEB_DB", str(Path.home() / ".local/state/ralf-teacher-web/student.sqlite3")))
    parser.add_argument("--port", type=int, default=19139)
    parser.add_argument("--demo", action="store_true", help="Explicit offline demo; health remains degraded")
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--seed-only", action="store_true", help="Exit after explicitly requested demo enrollment")
    parser.add_argument("--enroll", action="store_true", help="Operator-only local enrollment; credential via hidden terminal input")
    args = parser.parse_args()
    if args.seed_only and not args.seed_demo:
        parser.error("--seed-only requires --seed-demo")
    os.umask(0o077)
    state = State(args.db)
    teacher = DemoTeacher() if args.demo else TeacherClient()
    if args.enroll:
        card = input("Membership card: ")
        name = input("Display name: ")
        school = input("School level (primary/middle/upper): ")
        grade = int(input("Grade: "))
        track = input("School track (liceo/tecnico/professionale, upper only): ") if school == "upper" else ""
        state.register(card, getpass.getpass("Credential (at least 8 characters): "), name, school, grade, track)
        print("Student enrolled")
        return
    if args.seed_demo:
        application = LearningApplication(state, teacher)
        for card, name, school, grade, track in [("DEMO-PRIMARY", "Alex Demo", "primary", 4, ""), ("DEMO-MIDDLE", "Marco Demo", "middle", 2, ""), ("DEMO-UPPER", "Sam Demo", "upper", 2, "liceo")]:
            from ralfloop_agent.teacher.web.state import digest
            with state.connect() as conn:
                exists = conn.execute("SELECT 1 FROM students WHERE membership_card_id=?", (digest("teacher-web-card:" + card),)).fetchone()
            if exists: continue
            sid = state.register(card, "StudioDemo!2026", name, school, grade, track, demo=True)
            student = state.authenticate(state.login(card, "StudioDemo!2026"))
            text = "Le frazioni rappresentano parti uguali di un intero. Numeratore e denominatore moltiplicati per lo stesso numero danno frazioni equivalenti. Una metà equivale a due quarti." if school != "upper" else "Nel moto uniforme la distanza è velocità per tempo. A velocità di due metri al secondo, in tre secondi si percorrono sei metri."
            application.add_material(student, "Quaderno demo — testo originale autorizzato", text, "own", "book", "Capitolo 1", "1")
        print("Demo profiles available; see docs/teacher-web.md for demo credentials")
        if args.seed_only:
            return
    import uvicorn
    origin = os.getenv("TEACHER_WEB_ORIGIN", f"http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(state, teacher, origin=origin, secure_cookie=origin.startswith("https://")), host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
