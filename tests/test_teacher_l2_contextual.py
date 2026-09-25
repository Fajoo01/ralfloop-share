from ralfloop_agent.teacher.grammar_client import contextual_l2_checks


def verb(lemma: str, modo: str, *, tempo: str = "presente", numero: str = "", genere: str = "") -> dict:
    features = {"lemma": lemma, "modo": modo, "tempo": tempo}
    if numero:
        features["numero"] = numero
    if genere:
        features["genere"] = genere
    return {"category": "verbo", "features": features}


def forms(lemma: str) -> list[dict]:
    return [
        {"token": "andato", "category": "verbo", "features": {"lemma": lemma, "modo": "participio", "tempo": "passato", "numero": "singolare", "genere": "maschile"}},
        {"token": "andata", "category": "verbo", "features": {"lemma": lemma, "modo": "participio", "tempo": "passato", "numero": "singolare", "genere": "femminile"}},
        {"token": "andati", "category": "verbo", "features": {"lemma": lemma, "modo": "participio", "tempo": "passato", "numero": "plurale", "genere": "maschile"}},
        {"token": "andate", "category": "verbo", "features": {"lemma": lemma, "modo": "participio", "tempo": "passato", "numero": "plurale", "genere": "femminile"}},
    ]


def test_flags_real_l2_auxiliary_infinitive_and_filters_subject_number():
    checks = contextual_l2_checks(
        "io ieri sono andare lavoro?",
        analyses_by_token={
            "sono": [verb("essere", "indicativo")],
            "andare": [verb("andare", "infinito")],
        },
        participle_forms={"andare": forms("andare")},
    )
    assert len(checks["issues"]) == 1
    assert checks["issues"][0]["candidate_participles"] == ["andato", "andata"]
    assert not checks["ambiguous"]


def test_does_not_autocorrect_copular_infinitive():
    checks = contextual_l2_checks(
        "il problema è andare via",
        analyses_by_token={
            "è": [verb("essere", "indicativo")],
            "andare": [verb("andare", "infinito")],
        },
        participle_forms={"andare": forms("andare")},
    )
    assert checks["issues"] == []
    assert checks["ambiguous"][0]["reason"] == "copular_infinitive_possible"


def test_flags_essere_plus_infinitive_with_overt_personal_subject():
    checks = contextual_l2_checks(
        "lui è andare a casa",
        analyses_by_token={
            "è": [verb("essere", "indicativo")],
            "andare": [verb("andare", "infinito")],
        },
        participle_forms={"andare": forms("andare")},
    )
    assert len(checks["issues"]) == 1
    assert checks["issues"][0]["candidate_participles"] == ["andato"]


def test_suppresses_quoted_and_metalinguistic_mentions():
    quoted = contextual_l2_checks(
        "La forma «sono andare» è sbagliata.",
        analyses_by_token={
            "sono": [verb("essere", "indicativo")],
            "andare": [verb("andare", "infinito")],
        },
        participle_forms={"andare": forms("andare")},
    )
    assert quoted["issues"] == []
    assert len(quoted["suppressed"]) == 1

    plain = contextual_l2_checks(
        "la frase sono andare è sbagliata",
        analyses_by_token={
            "sono": [verb("essere", "indicativo")],
            "andare": [verb("andare", "infinito")],
        },
        participle_forms={"andare": forms("andare")},
    )
    assert plain["issues"] == []
    assert len(plain["suppressed"]) == 1


def test_avere_plus_infinitive_is_high_confidence_and_uses_base_participle():
    checks = contextual_l2_checks(
        "ieri ho mangiare pizza",
        analyses_by_token={
            "ho": [verb("avere", "indicativo")],
            "mangiare": [verb("mangiare", "infinito")],
        },
        participle_forms={
            "mangiare": [
                {"token": "mangiata", "category": "verbo", "features": {"lemma": "mangiare", "modo": "participio", "tempo": "passato", "numero": "singolare", "genere": "femminile"}},
                {"token": "mangiato", "category": "verbo", "features": {"lemma": "mangiare", "modo": "participio", "tempo": "passato", "numero": "singolare", "genere": "maschile"}},
            ]
        },
    )
    assert checks["issues"][0]["candidate_participles"] == ["mangiato"]
