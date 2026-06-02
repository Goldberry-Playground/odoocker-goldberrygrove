import io

from sanitize_dump import sanitize_stream


def test_res_partner_columns_rewritten(fixture_loader):
    input_sql = fixture_loader("input_partner.sql")
    expected = fixture_loader("expected_partner.sql")

    output = io.StringIO()
    sanitize_stream(io.StringIO(input_sql), output)

    assert output.getvalue() == expected
