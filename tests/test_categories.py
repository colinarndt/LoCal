from local_calendar import prompts, web


def test_theater_is_available_to_extraction_and_filters():
    choices = prompts.EXTRACT_SCHEMA["properties"]["category"]["anyOf"][0]["enum"]

    assert prompts.EXTRACT_PROMPT_VERSION == "extract-v3"
    assert "theater" in choices
    assert "theater" in web.CATEGORIES
    assert "Plays, musicals" in prompts.EXTRACT_SYSTEM_TEMPLATE
