from app.admin_web import (
    _lead_voice_extract_basic_from_transcript,
    _lead_voice_extract_name_from_transcript,
    _lead_voice_normalise_spoken_email,
)


def test_lead_voice_preserves_yahoo_uk_domain():
    assert _lead_voice_normalise_spoken_email("tracycottle73@yahoo.co.uk") == "tracycottle73@yahoo.co.uk"


def test_lead_voice_spelled_email_local_part_replaces_misheard_word():
    spoken = "mama sitter, m-a-m-a-s-i-t-t-a at btinternet dot com"
    assert _lead_voice_normalise_spoken_email(spoken) == "mamasitta@btinternet.com"


def test_lead_voice_extracts_leading_name_when_ai_missed_it():
    transcript = (
        "Rogers number 07739435497. Email smellyce.jr@gmail.com. "
        "Source CheckerTrade, job type gutter clean."
    )
    lead = _lead_voice_extract_basic_from_transcript(transcript, {}, for_edit=False)
    assert lead["lead_name"] == "Rogers"
    assert lead["email"] == "smellyce.jr@gmail.com"


def test_lead_voice_uses_spelled_name_parts():
    transcript = (
        "Customer name, Sina, that's spelled S-I-N-A. "
        "Zan Garner, spelled Z-A-N-G-A-N-A. Number 07884262329."
    )
    assert _lead_voice_extract_name_from_transcript(transcript) == "Sina Zangana"


def test_lead_voice_spelled_surname_keeps_first_name():
    transcript = (
        "27th of September, name Matthew Merritt, spelt M-E-R-R-E-T-T, "
        "number 07766661323, email MatthewMerritt@hotmail.co.uk"
    )
    assert _lead_voice_extract_name_from_transcript(transcript) == "Matthew Merrett"


def test_lead_voice_spelled_name_wins_over_email_spelling():
    transcript = (
        "27th of September, name Matthew Merritt, spelt M-E-R-R-E-T-T, "
        "number 07766661323, email MatthewMerritt@hotmail.co.uk"
    )
    lead = _lead_voice_extract_basic_from_transcript(transcript, {}, for_edit=False)
    assert lead["lead_name"] == "Matthew Merrett"
    assert lead["email"] == "matthewmerritt@hotmail.co.uk"
