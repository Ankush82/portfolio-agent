import pytest
from src.webapp import create_app
from unittest.mock import patch


@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_stock_entry_page_loads(client):
    """Test that the stock entry page loads and contains the required helper text."""
    response = client.get('/stock_entry')
    assert response.status_code == 200
    assert b'For Indian stocks, add .NS (NSE) or .BO (BSE) suffix (e.g., RELIANCE.NS). Symbols are case-sensitive.' in response.data


def test_validate_symbol_valid_indian_symbol(client):
    """Test that a valid Indian stock symbol returns exchange and currency."""
    with patch('src.webapp.fetch_yahoo_finance_quote') as mock_fetch:
        mock_fetch.return_value = {
            'exchange_name': 'NSE',
            'currency': 'INR'
        }
        response = client.post('/validate_symbol', json={'symbol': 'RELIANCE.NS'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['exchange'] == 'NSE'
        assert data['currency'] == 'INR'


def test_validate_symbol_invalid_format(client):
    """Test that an invalid format returns an error."""
    response = client.post('/validate_symbol', json={'symbol': 'reliance.ns'})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data


def test_validate_symbol_not_found(client):
    """Test that a symbol not found returns an error."""
    with patch('yahoo_finance_client.fetch_yahoo_finance_quote') as mock_fetch:
        mock_fetch.side_effect = Exception('Symbol not found')
        response = client.post('/validate_symbol', json={'symbol': 'INVALID.NS'})
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data


def test_template_contains_required_elements(client):
    """Test that the template contains all required UI elements."""
    response = client.get('/stock_entry')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    # Helper text
    assert 'For Indian stocks, add .NS (NSE) or .BO (BSE) suffix (e.g., RELIANCE.NS). Symbols are case-sensitive.' in html
    # Loading state
    assert 'Verifying symbol...' in html
    # Success state placeholders
    assert 'id="exchange-display"' in html
    assert 'id="currency-display"' in html
    # Error state
    assert 'id="server-error-message"' in html


def test_validate_symbol_valid_bse_symbol(client):
    """Test that a valid BSE stock symbol returns exchange and currency."""
    with patch('yahoo_finance_client.fetch_yahoo_finance_quote') as mock_fetch:
        mock_fetch.return_value = {
            'exchange_name': 'BSE',
            'currency': 'INR'
        }
        response = client.post('/validate_symbol', json={'symbol': '123456.BO'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['exchange'] == 'BSE'
        assert data['currency'] == 'INR'


def test_validate_symbol_invalid_bse_format(client):
    """Test that an invalid BSE format returns an error."""
    # Test invalid length
    response = client.post('/validate_symbol', json={'symbol': '12345.BO'})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    # Test non-digits
    response = client.post('/validate_symbol', json={'symbol': '12345A.BO'})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    # Test lowercase suffix
    response = client.post('/validate_symbol', json={'symbol': '123456.bo'})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data