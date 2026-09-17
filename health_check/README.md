# Health Check System Documentation

## Overview

The Health Check System is designed to monitor the health and functionality of various API endpoints in the Sharemundo backend. It runs automated tests against predefined endpoints and stores the results in the database for analysis and monitoring.

## Architecture

### Data Storage

The system uses two main models to store health check data:

1. **APITestRun** - Represents a complete test run session
2. **APITestResult** - Stores individual test results for each JSON file

### Models Structure

```python
class APITestRun(models.Model):
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(choices=['started', 'finished', 'failed'])

class APITestResult(models.Model):
    file = models.CharField(max_length=255)  # JSON file name
    checked_at = models.DateTimeField()
    results = models.JSONField()  # Array of endpoint test results
    api_test_run = models.ForeignKey(APITestRun, on_delete=models.CASCADE)
```

## API Endpoints

### 1. Run Health Check Tests
```
GET /api/health-check/status/test/
```
- **Purpose**: Executes all health check tests
- **Authentication**: Requires API key
- **Response**: Returns test results for all JSON files
- **Frequency**: Runs every hour via scheduled job

### 2. Get Last Test Results
```
GET /api/health-check/last-check/
```
- **Purpose**: Retrieves results from the most recent test run
- **Authentication**: Requires API key
- **Response**: Paginated results from the latest test run

### 3. Get Historical Data
```
GET /api/health-check/last-check/history/
```
- **Purpose**: Retrieves historical health check data
- **Query Parameters**:
  - `days` (int): Number of days to look back (default: 7)
  - `status` (str): Filter by test run status ('started', 'finished', 'failed')
  - `file` (str): Filter by specific JSON file name
- **Response**: Historical test runs with detailed results

### 4. Get Summary Statistics
```
GET /api/health-check/last-check/summary/
```
- **Purpose**: Provides summary statistics and analytics
- **Query Parameters**:
  - `days` (int): Number of days to analyze (default: 7)
- **Response**: 
  - Overall success rates
  - File-wise statistics
  - Recent test runs
  - Performance metrics

### 5. Get Specific Test Run Details
```
GET /api/health-check/last-check/{id}/details/
```
- **Purpose**: Get detailed results for a specific test run
- **Response**: Complete breakdown of all test results for that run

## Test Files Structure

The system tests endpoints defined in JSON files located in `health_check/test_data/`:

### Supported Test Files:
- `auth.json` - Authentication endpoints
- `company_profiles.json` - Company profile endpoints
- `company_config.json` - Company configuration endpoints
- `notice_board.json` - Notice board endpoints
- `opportunity.json` - Opportunity endpoints
- `human_resource_staff.json` - Staff management endpoints
- `human_resource_staff_observation.json` - Staff observation endpoints
- `human_resource_leave_requests.json` - Leave request endpoints
- `project.json` - Project endpoints
- `project_phase.json` - Project phase endpoints
- `milestone.json` - Milestone endpoints
- `contact_company.json` - Contact company endpoints
- `contact_user.json` - Contact user endpoints
- `action_items.json` - Action item endpoints
- `expense_card.json` - Expense card endpoints
- `site_reporting.json` - Site reporting endpoints
- `time_card.json` - Time card endpoints
- `events.json` - Calendar events endpoints
- `feedback.json` - Feedback endpoints
- `chat.json` - Chat endpoints
- `invoice.json` - Invoice endpoints
- `document.json` - Document endpoints

### JSON File Format:
```json
[
  {
    "url": "/api/endpoint/",
    "method": "GET",
    "expected_status": 200
  },
  {
    "url": "/api/endpoint/",
    "method": "POST",
    "payload_name": "payload_key",
    "expected_status": 201
  }
]
```

**Note**: URLs in JSON files now use only paths (starting with `/`). The base URL is configured via the `HEALTH_CHECK_BASE_URL` environment variable and is automatically prepended to all paths.

## Scheduled Execution

The health check system runs automatically every hour using Django management commands:

### Management Command
```bash
python manage.py health_check_job
```

### Cron Job Configuration
The command is typically scheduled to run every 59 minutes to ensure continuous monitoring.

## Data Flow

1. **Scheduled Trigger**: Cron job triggers health check every hour
2. **Test Execution**: System processes each JSON file sequentially
3. **Data Storage**: Results are stored in `APITestRun` and `APITestResult` tables
4. **API Access**: Stored data can be retrieved via REST APIs
5. **Monitoring**: Historical data provides insights into system health trends

## Key Features

### Sequential Processing
- JSON files are processed one by one to ensure proper data dependencies
- Global ID tracking ensures created objects are available for subsequent tests
- 2-second delays between files prevent system overload

### Dependency Management
- Tests are ordered to handle dependencies (e.g., milestone created before action items)
- Fallback mechanisms ensure tests can run even if dependencies fail
- Database lookups provide existing data when new creation fails

### Comprehensive Analytics
- Success rate calculations for individual endpoints and files
- Historical trend analysis
- Performance metrics and statistics
- Detailed error reporting and debugging information

## Usage Examples

### Get Last 7 Days Summary
```bash
curl -H "X-API: your_api_key" \
     "http://localhost:8000/api/health-check/last-check/summary/?days=7"
```

### Get Historical Data for Specific File
```bash
curl -H "X-API: your_api_key" \
     "http://localhost:8000/api/health-check/last-check/history/?file=action_items&days=30"
```

### Get Specific Test Run Details
```bash
curl -H "X-API: your_api_key" \
     "http://localhost:8000/api/health-check/last-check/123/details/"
```

## Configuration

### Environment Variables
- `HEALTH_CHECK_BASE_URL`: Base URL for all health check endpoints (e.g., `http://localhost:8000`)
- `HEALTH_CHECK_API_KEY`: API key for authentication
- `HEALTH_CHECK_AUTH0_USERNAME`: Auth0 username for testing
- `HEALTH_CHECK_AUTH0_PASSWORD`: Auth0 password for testing
- `HEALTH_CHECK_AUTH0_REALM`: Auth0 realm (default: 'DEVELOPMENT-DATABASE')

### Settings
```python
# settings/common.py
HEALTH_CHECK_API_KEY = env("HEALTH_CHECK_API_KEY", default="health_check_secret_key_2024")
HEALTH_CHECK_BASE_URL = env("HEALTH_CHECK_BASE_URL", default="http://localhost:8000")
```

### Environment Configuration Examples

#### Development
```bash
HEALTH_CHECK_BASE_URL=http://localhost:8000
```

#### Staging
```bash
HEALTH_CHECK_BASE_URL=https://staging-api.sharemundo.com
```

#### Production
```bash
HEALTH_CHECK_BASE_URL=https://api.sharemundo.com
```

## Monitoring and Alerts

The system provides comprehensive monitoring capabilities:

1. **Real-time Status**: Check current system health
2. **Trend Analysis**: Monitor performance over time
3. **Failure Detection**: Identify problematic endpoints
4. **Success Rate Tracking**: Measure system reliability
5. **Historical Data**: Analyze patterns and improvements

## Troubleshooting

### Common Issues

1. **Milestone ID is None**: Ensure milestone.json is processed before action_items.json
2. **Authentication Failures**: Check Auth0 credentials and API key
3. **Database Connection Issues**: Verify database connectivity
4. **Rate Limiting**: Adjust delays between requests if needed

### Debug Information
The system provides extensive debug logging:
- File processing status
- Created object IDs
- Authentication status
- Error details and fallback mechanisms

## Future Enhancements

1. **Email Alerts**: Send notifications for failed tests
2. **Dashboard Integration**: Web-based monitoring dashboard
3. **Custom Test Suites**: Allow custom test configurations
4. **Performance Metrics**: Response time tracking
5. **Automated Recovery**: Self-healing mechanisms for failed endpoints 