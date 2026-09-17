
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging



# Set up logging
logger = logging.getLogger(__name__)

class ECBCurrencyConverter:
    """
    European Central Bank Currency Converter
    
    This class provides methods to fetch currency exchange rates from the ECB API
    and perform currency conversions using the latest available rates.
    """
    
    BASE_URL = "https://data-api.ecb.europa.eu/service/data"
    DATASET = "EXR"  # Exchange Rates dataset
    
    def __init__(self):
        self._cache = {}
        self._cache_timestamp = None
        self._cache_duration = timedelta(hours=1)  # Cache for 1 hour
    
    def get_conversion_rate(self, source_currency: str, target_currency: str) -> float:
        """
        Get the conversion rate between two currencies.
        
        Args:
            source_currency (str): Source currency code (e.g., 'EUR', 'USD')
            target_currency (str): Target currency code (e.g., 'USD', 'GBP')
            
        Returns:
            float: Conversion rate from source to target currency, or None if not found
        """
        try:
            # Handle EUR as base currency (ECB rates are EUR-based)
            if source_currency.upper() == 'EUR' and target_currency.upper() == 'EUR':
                return 1.0
            
            rates = self._get_latest_rates()
            
            if source_currency.upper() == 'EUR':
                # Direct conversion from EUR to target
                return rates.get(target_currency.upper())
            elif target_currency.upper() == 'EUR':
                # Inverse conversion from source to EUR
                source_rate = rates.get(source_currency.upper())
                if source_rate:
                    return 1.0 / source_rate
                return None
            else:
                # Cross-currency conversion via EUR
                source_rate = rates.get(source_currency.upper())
                target_rate = rates.get(target_currency.upper())
                
                if source_rate and target_rate:
                    # Convert source to EUR, then EUR to target
                    return target_rate / source_rate
                return None
                
        except Exception as e:
            logger.error(f"Error getting conversion rate: {e}")
            return None
    
    def convert_currency(self, amount: float, source_currency: str, target_currency: str) -> float:
        """
        Convert an amount from one currency to another.
        
        Args:
            amount (float): Amount to convert
            source_currency (str): Source currency code
            target_currency (str): Target currency code
            
        Returns:
            float: Converted amount, or None if conversion failed
        """
        rate = self.get_conversion_rate(source_currency, target_currency)
        if rate is not None:
            # Round to 2 decimal places for currency
            converted = Decimal(str(amount * rate)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            return float(converted)
        return None
    
    def _get_latest_rates(self):
        """
        Fetch the latest exchange rates from ECB API with caching.
        
        Returns:
            Dict[str, float]: Dictionary of currency codes to rates
        """
        # Check cache
        if (self._cache_timestamp and 
            datetime.now() - self._cache_timestamp < self._cache_duration and 
            self._cache):
            return self._cache
        
        try:
            # Construct API URL for latest daily rates
            url = f"{self.BASE_URL}/{self.DATASET}/D..EUR.SP00.A"
            
            # Add parameters for latest data
            params = {
                'lastNObservations': '1',  # Get only the latest observation
                'format': 'genericdata'    # XML format
            }
            
            headers = {
                'Accept': 'application/vnd.sdmx.genericdata+xml;version=2.1',
                'User-Agent': 'Python ECB Currency Converter'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            
            # Parse XML response
            rates = self._parse_rates_xml(response.content)
            
            # Update cache
            self._cache = rates
            self._cache_timestamp = datetime.now()
            
            return rates
            
        except requests.RequestException as e:
            logger.error(f"Request error: {e}")
            raise Exception(f"Failed to fetch exchange rates: {e}")
        except ET.ParseError as e:
            logger.error(f"XML parsing error: {e}")
            raise Exception(f"Failed to parse exchange rates response: {e}")
    
    def _parse_rates_xml(self, xml_content: bytes):
        """
        Parse XML response from ECB API to extract exchange rates.
        
        Args:
            xml_content (bytes): XML content from API response
            
        Returns:
            Dict[str, float]: Dictionary of currency codes to rates
        """
        rates = {}
        
        try:
            root = ET.fromstring(xml_content)
            
            # Define namespaces
            namespaces = {
                'message': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
                'generic': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic',
                'common': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
            }
            
            # Find all series in the dataset
            for series in root.findall('.//generic:Series', namespaces):
                currency_code = None
                
                # Extract currency code from series key
                for key in series.findall('generic:SeriesKey/generic:Value', namespaces):
                    if key.get('id') == 'CURRENCY':
                        currency_code = key.get('value')
                        break
                
                if currency_code:
                    # Get the latest observation value
                    obs = series.find('generic:Obs/generic:ObsValue', namespaces)
                    if obs is not None:
                        try:
                            rate = float(obs.get('value'))
                            rates[currency_code] = rate
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid rate value for {currency_code}")
            
            logger.info(f"Successfully parsed {len(rates)} exchange rates")
            return rates
            
        except ET.ParseError as e:
            logger.error(f"Failed to parse XML: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error parsing rates: {e}")
            raise


