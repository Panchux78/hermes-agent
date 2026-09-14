import pytest
from plugins.platforms.telegram.ccma_workbook import parse_amount

@pytest.mark.parametrize('text,expected',[
 ('22,307.45',22307.45),('22307.45',22307.45),('22.307,45',22307.45),('22307,45',22307.45),
 ('0.01',.01),('0,01',.01),('(22,307.45)',-22307.45),('1,234,567.89',1234567.89),
 ('1.234.567,89',1234567.89),('1.234',None),('1,234',None),('12,34.56',None)])
def test_decimal_conventions_preserve_magnitude(text,expected):
    assert parse_amount(text)==expected
