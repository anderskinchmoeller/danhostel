from datetime import date

from app.picasso_belaegning import on_the_books, parse

TEXT = """                                   Arrivals on period: 20-12-2026 to 05-01-2027
10401 BF1 G                             121314       22-12 15:00 25-12     3    1    1 ONLINE_NON-REF
51001 D2  C                             121315       31-12 15:00 02-01     2    2    4 YP   800,00
      D2  T                             121316       31-12 15:00 01-01     1    5    5 ROOM   0,00
      V8  X                             121317       31-12 15:00 01-01     1    1    8 ROOM   0,00
      V10 I                             121318       20-12 15:00 22-12     2    1    8 ROOM   0,00
"""


def test_year_rollover_and_status_filter():
    start, end, rows = parse(TEXT, today=date(2026, 12, 21))
    assert (start, end) == (date(2026, 12, 20), date(2027, 1, 5))
    otb = on_the_books(start, end, rows)
    assert otb[date(2026, 12, 23)] == (0, 1)       # BF1 er en seng
    assert otb[date(2026, 12, 31)] == (2, 0)       # tentativ og annulleret tæller ikke
    assert otb[date(2027, 1, 1)] == (2, 0)         # januar lander i 2027
    assert otb[date(2027, 1, 2)] == (0, 0)         # afrejsedagen tæller ikke
    assert otb[date(2026, 12, 20)] == (1, 0)       # in-house sovesal solgt samlet


def test_guest_names_between_type_and_ref():
    text = """   Arrivals on period: 22-08-2026 to 19-01-2027
51501  BV8  C Mendgaard, Peder-wilhelm111355            30-10  15:00  01-11    2     1    1  YP  451,80
       D1   I Sørensen/033, Henrik        084894        30-09  15:00  30-09  365     1    1       0,00
50302  BV6  C Oak/001, Louicka            112875/001    02-10  15:00  03-10    1     1    1  ROOM 200,00
"""
    start, end, rows = parse(text, today=date(2026, 9, 21))
    assert [r["ref"] for r in rows] == ["111355", "084894", "112875/001"]
    assert rows[1]["arrival"] == date(2025, 9, 30)   # in-house langtidsgæst
    otb = on_the_books(start, end, rows)
    assert otb[date(2026, 9, 21)] == (1, 0)
    assert otb[date(2026, 9, 29)] == (1, 0)          # afrejse 30-09 fra rapporten
    assert otb[date(2026, 9, 30)] == (0, 0)
    assert otb[date(2026, 10, 31)] == (0, 1)


def test_two_letter_room_type_is_not_split():
    text = """   Arrivals on period: 22-08-2026 to 19-01-2027
       FS   C  Duedahl, Lise               121020        25-09  15:00  26-09    1     1    8  YP 1.235,48
       BV10G  Forberg, Maria              122481        20-10  15:00  21-10    1     1    1  ONLINE 200,00
"""
    _, _, rows = parse(text, today=date(2026, 9, 21))
    assert [(r["type"], r["st"]) for r in rows] == [("FS", "C"), ("BV10", "G")]


def test_in_house_is_read_as_of_the_report_date_not_processing_date():
    """En rapport fra 21-09 behandlet 24-09: afrejser 22-09 må ikke rulle et år frem."""
    text = """   Status: Confirmed, In-House                                      LENE 21-09-2026 kl. 21:41
   Arrivals on period: 22-08-2026 to 19-01-2027
       D2   I Hansen, Anna                121000        19-09  15:00  22-09    3     1    2  YP   900,00
       F1   I Berg, Ole                   121001        20-09  15:00  21-09    1     1    2  YP   500,00
       BV8  I Dahlstrøm, Thor             121673        03-09  19:42  09-09    6     1    1  ROOM 1.080,00
       D1   I Sørensen/033, Henrik        084894        30-09  15:00  30-09  365     1    1       0,00
"""
    start, end, rows = parse(text)                       # ingen today: rapportens dato bruges
    otb = on_the_books(start, end, rows)
    assert otb[date(2026, 9, 21)] == (2, 0)              # D2 + D1; F1 rejser 21-09
    assert otb[date(2026, 9, 22)] == (1, 0)              # kun langtidsgæsten D1 tilbage
    assert otb[date(2026, 9, 30)] == (0, 0)              # D1 rejser 30-09
    assert otb[date(2026, 10, 15)] == (0, 0)             # ingen rullet et år frem
