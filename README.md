## Building and running your application (Through Docker)

1. Replace `DB_HOST` from `localhost` to `db` in .env.local file.

When you're ready, start your application by running: 
`docker compose up --build  

Your application will be available at http://localhost:8000  

## 🚀 Building and running your application (Locally)
 
### Start DB:
 
`brew install postgresql@14`  
`brew services start postgresql@14`
  
- Run "psql postgres" to enter into shell of db. 
- Run SQL commands in set_local_db.sql
 
Now your DB is up and ready to be connected by backend

### Start Django Backend Server: 

To get started with this Django project, follow these steps:<br>

1. Create a new virtual environment and activate it (`python3 -m venv backend-env && source ./backend-env/bin/activate`)
2. In the monorepo, install the required dependencies by running `pip install -r requirements.local.txt` in your terminal.
3. Run `python manage.py migrate` to create the database tables.
4. Start the development server by running `python manage.py runserver`.
5. The app will be up at `http://localhost:8000/`.

Note: This guide assumes that you have already installed Python and Django on your system. If you haven't, please refer to the official Django documentation for installation instructions.

Link for reference:
https://blog.nextideatech.com/how-to-create-a-django-app-and-connect-it-to-a-database/#:~:text=Django%2C%20being%20a%20modern%20framework,%2C%20Oracle%2C%20MongoDB%2C%20etc

**🚀 Backend Project Documentation**

---

# 🌐 Base URLs

- **Chats API**: `/api/chats/`
- **Company Management APIs**: `/api/`

---

## 📑 Table of Contents

1. [Chats](#chats)
2. [Company Management](#company-management)
   - [Company Profile & Users](#company-profile--users)
   - [Company Search](#company-search)
   - [Company Configuration](#company-configuration)
   - [Opportunity Management](#opportunity-management)
   - [Notice Board](#notice-board)

---

## 💬 Chats

**Base URL**: `/api/chats/`

### 🔹 Features

- ✨ **Real-Time Messaging**: Instant delivery via WebSockets for smooth interactions.
- 🔄 **Live CRUD Actions**: Create, read, update, and delete messages with live broadcast to connected clients.
- 🚀 **Redis Channel Layer**: Scalable event broadcasting for high-concurrency environments.
- 🔒 **Secure Connections**: Authenticated users only, ensuring private and safe communications.
- 🛠️ **DRF Integration**: RESTful endpoints for all chat operations alongside WebSockets.
- 📚 **Persistent History**: Complete chat logs stored for retrieval and auditing.
- 📈 **Scalable Architecture**: Built to grow from small teams to enterprise deployments.

### 🧩 Multi-Level Chat Structure

#### 🗂 Project-Level Chat

- Scoped conversation for project teams.
- Visible only to assigned project members.
- Ideal for task discussions, sprint updates, and focused collaboration.

#### 🏢 Company-Level Chat

- Organization-wide communication channel.
- Perfect for company announcements, cross-team brainstorming, and social engagement.

### 🔌 ChatRoom Endpoints (`ChatRoomViewSet`)

| Method | Path               | Description                                                  |
| :----: | ------------------ | ------------------------------------------------------------ |
|  GET   | `/chatrooms/`      | List direct chat rooms for the authenticated user.           |
|  POST  | `/chatrooms/`      | Create or retrieve a one-to-one chat room between two users. |
|  GET   | `/chatrooms/{id}/` | Fetch details of a specific chat room.                       |
|  PUT   | `/chatrooms/{id}/` | Replace chat room data (e.g., rename, participants).         |
| PATCH  | `/chatrooms/{id}/` | Partially update chat room metadata.                         |
| DELETE | `/chatrooms/{id}/` | Remove a chat room (and its messages).                       |

### ✉️ Message Endpoints (`MessageViewSet`)

| Method | Path              | Description                                         |
| :----: | ----------------- | --------------------------------------------------- |
|  GET   | `/messages/`      | Retrieve and paginate messages visible to the user. |
|  POST  | `/messages/`      | Send a new message (validates room membership).     |
|  GET   | `/messages/{id}/` | Retrieve details of a single message.               |
|  PUT   | `/messages/{id}/` | Update a message (e.g., change status or content).  |
| PATCH  | `/messages/{id}/` | Partially update message fields.                    |
| DELETE | `/messages/{id}/` | Delete a message from a chat room.                  |

---

## 🏢 Company Management

🔹 **Features**

- 👥 **Role-Based Access**: Fine-grained permissions for freelancers, company users, and admins.
- 🔍 **Search & Filter**: Full-text search plus field-based filters using DRF and Django Filter.
- ⚙️ **Config Management**: View, update, reset, and sync per-company settings with global defaults.
- 📊 **Opportunity Lifecycle**: CRUD for sales opportunities with comprehensive validation and history.
- 📝 **Notice Board**: Create and manage time-bound notices, auto-expiring when outdated.
- 🔄 **Dynamic Serializer Context**: Context-aware inclusion of history, bank details, and chat links based on user role.

### 🏢 Company Profile & Users

**CompanyProfileViewSet**  
| Method | Path | Description |
|:------:|-----------------------|-------------------------------------------------------------|
| GET | `/profile/` | Retrieve the authenticated user’s company profile. |
| GET | `/profile/users/` | List all active users within the company. |

> 🚫 _Freelancers receive a PermissionDenied error._

---

### 🔍 Company Search

**CompanySearchView**  
| Method | Path | Description |
|:------:|-----------------------|-------------------------------------------------------------|
| GET | `/company-search/` | Search companies with query params; excludes own company. |

**Query Params**

- `search` : Full-text across name, services, and expertise.
- `company_country`, `company_town`, `expertise` : Case-insensitive filters.

---

### ⚙️ Company Configuration

**CompanyConfigViewSet**  
| Method | Path | Description |
|:------:|---------------------------------------|--------------------------------------------------------------|
| GET | `/company-config/` | List visible configs (company or freelancer). |
| GET | `/company-config/{id}/` | Retrieve a specific config. |  
| PUT | `/company-config/{id}/` | Update config (admin only for company users). |
| PATCH | `/company-config/{id}/` | Partially update config fields. |
| POST | `/company-config/` | **Not allowed** (405). |
| DELETE | `/company-config/{id}/` | **Not allowed** (405). |
| POST | `/company-config/reset/` | Reset settings to global defaults (admin only). |
| POST | `/company-config/sync/` | Sync from GlobalConfig (admin only). |

---

### 💼 Opportunity Management

**OpportunityViewSet**  
| Method | Path | Description |
|:------:|---------------------------------------|--------------------------------------------------------------|
| GET | `/opportunity/` | List opportunities for freelancer or company. |
| POST | `/opportunity/` | Create a new opportunity (auto-assign creator). |
| GET | `/opportunity/{id}/` | Retrieve opportunity details (permission-checked). |
| PUT | `/opportunity/{id}/` | Update opportunity with validation on status & docs. |
| PATCH | `/opportunity/{id}/` | Partially update an opportunity. |
| DELETE | `/opportunity/{id}/` | Delete opportunity and its history. |

> **Query Param**: `include_history=true` to include chronological changes.

---

### 📝 Notice Board

**NoticeBoardViewSet**  
| Method | Path | Description |
|:------:|----------------------------------|-----------------------------------------------------------|
| GET | `/notice-board/` | List all active notices (expires_at ≥ today). |
| POST | `/notice-board/` | Create a new notice (admin only). |
| GET | `/notice-board/{id}/` | Retrieve notice details (permission-checked). |
| PUT | `/notice-board/{id}/` | Update a notice (admin only). |
| PATCH | `/notice-board/{id}/` | Partially update a notice. |
| DELETE | `/notice-board/{id}/` | Delete a notice (admin only). |

---

## 👥 Human Resource Management

**Base URL**: `/api/`

🔹 **Features**

- 📝 **Leave Requests**: Employees can submit and track leave or work-from-home requests.
- 📧 **Email Notifications**: Automated notifications to managers and employees with templated HTML emails.
- 🧮 **Leave Calculation**: Accurate leave day count using business day logic.
- 🛠 **Staff Observations**: Create, view, and export observational notes tied to staff.
- ⚙️ **Staff Configuration**: Manage custom employee-level settings or categories.

---

### 📝 Leave Requests

**LeaveRequestViewSet**  
| Method | Path | Description |
|:------:|-----------------------------|----------------------------------------------------------------|
| GET | `/leave-requests/` | List leave requests visible to the user. |
| POST | `/leave-requests/` | Submit a new leave or work-from-home request. |
| GET | `/leave-requests/{id}/` | View specific leave request details. |
| PATCH | `/leave-requests/{id}/` | Approve or reject a leave request (if manager). |
| DELETE | `/leave-requests/{id}/` | Delete a pending or rejected request. |

---

### 👨‍💼 Staff Data

**StaffViewSet**  
| Method | Path | Description |
|:------:|--------------------|-----------------------------------------------------------|
| GET | `/staff/` | List staff members under a company. |
| GET | `/staff/export/` | Export staff leave data as an XLSX file (admin only). |

---

### 🕵️ Staff Observation

**StaffObservationViewSet**  
| Method | Path | Description |
|:------:|------------------------------|-------------------------------------------------------|
| GET | `/staff-observation/` | List staff observations visible to the requester. |
| POST | `/staff-observation/` | Add a new observation for a staff member. |
| GET | `/staff-observation/{id}/` | Retrieve details of a specific observation. |
| DELETE | `/staff-observation/{id}/` | Delete an observation (author only). |

---

### ⚙️ Staff Configuration

**StaffConfigurationViewSet**  
| Method | Path | Description |
|:------:|----------------------------------|-----------------------------------------------------|
| GET | `/staff-configuration/` | List staff-level config entries. |
| POST | `/staff-configuration/` | Create a new configuration entry. |
| GET | `/staff-configuration/{id}/` | Retrieve a configuration item. |
| PATCH | `/staff-configuration/{id}/` | Partially update config (admin/staff only). |
| DELETE | `/staff-configuration/{id}/` | Delete a config entry. |

---

## 👤 User Management & Calendar

🔹 **Features**

- 👥 **User Profiles**: Retrieve and update own user details with role-based permissions.
- 🔍 **User Search**: Filter and search freelancers and company users by location, skills, and keywords.
- 🌐 **Associated Connections**: Fetch users connected via company or freelancer relationships.
- 🖥️ **Dashboard Configuration**: Create, update, and delete per-user dashboard layouts.
- 📆 **Calendar Sync**: Integrate with Microsoft Outlook—authorize, fetch, sync, and manage calendar events.
- 🔔 **Event Scheduling**: Automatic reminders and status updates, with SQS-based notification scheduling.

### 👥 UserInfo Endpoints (`UserInfoViewSet`)

| Method | Path                                          | Description                                                  |
| :----: | --------------------------------------------- | ------------------------------------------------------------ |
|  GET   | `/user_info/`                                 | List users (company-wide for admins, self for others).       |
|  GET   | `/user_info/{id}/`                            | Retrieve a specific user (permission-checked).               |
|  PUT   | `/user_info/{id}/`                            | Full update of user info (self only).                        |
| PATCH  | `/user_info/{id}/`                            | Partial update of user info (self only).                     |
|  GET   | `/user_info/{id}/projects_with_action_items/` | Projects and assigned action items for a user.               |
|  GET   | `/user_info/get_associated_users/`            | Fetch all active connected users (companies or freelancers). |

### 🔎 User Search (`UserSearchView`)

| Method | Path           | Description                                                 |
| :----: | -------------- | ----------------------------------------------------------- |
|  GET   | `/user-search` | List searchable users, with filter and search query params. |

**Filter & Search Params**

- `city`, `country`, `qualifications`, `keywords`, `professional_headline`
- `search` (first_name, last_name, links_id, headline, position)

### 📊 Dashboard Configuration (`UserDashboardConfigurationViewSet`)

| Method | Path                          | Description                             |
| :----: | ----------------------------- | --------------------------------------- |
|  GET   | `/user-dashboad-config/`      | List current user’s dashboard settings. |
|  POST  | `/user-dashboad-config/`      | Create dashboard config (one per user). |
|  PUT   | `/user-dashboad-config/{id}/` | Update dashboard config (owner only).   |
| PATCH  | `/user-dashboad-config/{id}/` | Partial update (owner only).            |
| DELETE | `/user-dashboad-config/{id}/` | Delete dashboard config (owner only).   |

### 📅 Calendar Events (`CalendarEventViewSet`)

| Method | Path                                                 | Description                                                         |
| :----: | ---------------------------------------------------- | ------------------------------------------------------------------- |
|  GET   | `/events/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | List events, milestones, action items, and leave within date range. |
|  POST  | `/events/`                                           | Create a new calendar event (auto-assigns based on role).           |
|  GET   | `/events/{id}/`                                      | Retrieve event details (permission-checked).                        |
|  PUT   | `/events/{id}/`                                      | Update event (reschedules notifications if changed).                |
| PATCH  | `/events/{id}/`                                      | Partial update of event fields.                                     |
| DELETE | `/events/{id}/`                                      | Delete event and cancel reminders.                                  |

### 🔐 Outlook OAuth & Sync (`MicrosoftAuthViewSet`)

| Method | Path                           | Description                                            |
| :----: | ------------------------------ | ------------------------------------------------------ |
|  GET   | `/auth/outlook/auth/`          | Redirect to Microsoft login for calendar access.       |
|  GET   | `/auth/outlook/callback/`      | Handle OAuth callback, store tokens, and initial sync. |
|  POST  | `/auth/outlook/sync_calendar/` | Manually trigger calendar sync (requires valid token). |

---

## 🗂 Project Management Module

🔹 **Base URL**: `/api/`  
All endpoints below are registered via the `OptionalSlashRouter`, allowing trailing-slash flexibility.

---

### 🏗️ Core Resources & ViewSets

|      **Resource**      |        **ViewSet**        | **Endpoint**          | **Supported Methods**                                                                                                |
| :--------------------: | :-----------------------: | :-------------------- | :------------------------------------------------------------------------------------------------------------------- |
|      **Contacts**      |   `ContactUserViewSet`    | `/contact-user/`      | GET (list), POST (create), GET `/contact-user/{id}/`, PUT/PATCH, DELETE, import/export CSV                           |
|                        |  `ContactCompanyViewSet`  | `/contact-company/`   | same as above                                                                                                        |
|      **Projects**      |     `ProjectViewSet`      | `/projects/`          | GET, POST, GET/PUT/PATCH/DELETE `/projects/{id}/`, custom actions: `stats`, `landing-screen`, `funding-screen`       |
|   **Project Config**   |   `ECPMPConfigViewSet`    | `/ecpmpconfig/`       | GET, POST, GET/PUT/PATCH/DELETE `/ecpmpconfig/{id}/`                                                                 |
|     **Milestones**     |    `MilestoneViewSet`     | `/milestone/`         | GET, POST, GET/PUT/PATCH/DELETE `/milestone/{id}/`                                                                   |
|    **Action Items**    |    `ActionItemViewSet`    | `/action_items/`      | GET, POST, GET/PUT/PATCH/DELETE `/action_items/{id}/` , list filters & date-range via Swagger                        |
| **Object Assessments** | `ObjectAssessmentViewSet` | `/object_assessment/` | GET, POST, GET/PUT/PATCH/DELETE `/object_assessment/{id}/`, folder-like listing via `get_folder_like_structure=true` |
|   **Site Reporting**   |    `SiteReportViewSet`    | `/site-reporting/`    | GET, POST, GET/PUT/PATCH/DELETE `/site-reporting/{id}/` , search by `name`, `project`                                |
|   **Project Phases**   |   `ProjectPhaseViewSet`   | `/project-phase/`     | GET, POST, GET/PUT/PATCH/DELETE `/project-phase/{id}/`                                                               |
|  **Project Funding**   |  `ProjectFundingViewSet`  | `/project-funding/`   | GET, POST, GET/PUT/PATCH/DELETE `/project-funding/{id}/`                                                             |
|   **Time Tracking**    |   `ProjectTimeViewSet`    | `/project-time/`      | GET, POST, GET/PUT/PATCH/DELETE `/project-time/{id}/`                                                                |
|     **Time Cards**     |     `TimeCardViewSet`     | `/time-card/`         | GET, POST, GET/PUT/PATCH/DELETE `/time-card/{id}/`, filter by `invoice_status` via Swagger                           |
|   **Expense Cards**    |   `ExpenseCardViewSet`    | `/expense-card/`      | same as Time Cards, with approval-email workflow and invoice-status filtering                                        |
| **Billing & Invoices** |     `InvoiceViewSet`      | `/billing-invoice/`   | GET, POST, GET/PUT/PATCH/DELETE `/billing-invoice/{id}/`, list filters via Swagger                                   |
|      **Folders**       |      `FolderViewSet`      | `/folder/`            | GET, POST, GET/PUT/PATCH/DELETE `/folder/{id}/`, custom SEARCH `/folder/search?name=…`                               |
|     **Documents**      |     `DocumentViewSet`     | `/document/`          | GET, POST, GET/PUT/PATCH/DELETE `/document/{id}/`, filter by folder/project/company/freelancer                       |

---

### 🔄 Custom Actions & Background Tasks

|            **Action**             |             **ViewSet / APIView**              | **Endpoint**                                                                | **Notes**                                                                            |
| :-------------------------------: | :--------------------------------------------: | :-------------------------------------------------------------------------- | :----------------------------------------------------------------------------------- |
|    **Import Contacts (Excel)**    | `ContactUserViewSet` / `ContactCompanyViewSet` | `/contact-user/import-excel-file/`<br>`/contact-company/import-excel-file/` | Bulk create/update via pandas, atomic transaction                                    |
|     **Export Contacts (CSV)**     |                 same as above                  | `/contact-user/export-csv/`<br>`/contact-company/export-csv/`               | Streams CSV writer                                                                   |
|         **Upload to S3**          |       `ProjectViewSet.upload_file_to_s3`       | `/projects/upload-file-to-s3/`                                              | Validates SECRET_TOKEN, uses Boto3, returns `s3_url`                                 |
|       **Azure OAuth Login**       |              `AzureLoginAPIView`               | `/azure/login`                                                              | Generates `state`, builds Azure OAuth URL                                            |
|     **Azure OAuth Callback**      |             `AzureCallbackAPIView`             | `/azure/callback`                                                           | Exchanges code for tokens, stores `AzureToken`, redirects to front-end               |
|     **List SharePoint Sites**     |         `FetchSharePointSitesAPIView`          | `/azure/list-sites`                                                         | GET Graph API `/sites/?search=*`, returns list                                       |
|     **Sync SharePoint Files**     |         `FetchSharePointFilesAPIView`          | `/azure/update-sites-data`                                                  | POST site/project/freelancer/company, queues `fetch_sharepoint_data` Celery task     |
|      **Refresh Azure Token**      |           `AzureTokenRefreshAPIView`           | `/azure/refresh-token`                                                      | POST refresh if expired, updates `AzureToken`                                        |
|  **Connection Revoke Handling**   |       `UpdateProjectOnConnectionRevoke`        | `/update-project-on-connection-revoke`                                      | Validates SECRET_TOKEN, reassigns projects/timecards on connection revoke            |
|  **Remove User-Related Objects**  |       `RemoveRelatedObjectsOnUserDelete`       | `/remove-related-objects-on-user-delete`                                    | Deletes contacts, projects, team members, time/expense entries in atomic transaction |
| **Create Freelancer Scaffolding** |    `CreateRelatedObjectsOnFreelancerCreate`    | `/create-related-objects-on-freelancer-create`                              | Builds freelancer root folder + subfolders, chatroom                                 |
|  **Create Company Scaffolding**   |     `CreateRelatedObjectsOnCompanyCreate`      | `/create-related-objects-on-company-create`                                 | Builds company root folder + subfolders, chatroom, updates admin staff supervisor    |
|   **Create User-Related Reads**   |       `CreateRelatedObjectsOnUserCreate`       | `/create-related-objects-on-user-create`                                    | Bulk-creates `MessageRead` entries for existing company messages                     |

---

### 🔧 Shared Utilities & Filters

- **Filtering**:
  - `MilestoneFilter`, `ActionItemFilter`, `ObjectAssessmentFilter`, `ProjectFilter`, `ContactUserFilter`, `ContactCompanyFilter`, `BillingInvoiceFilter` handle date ranges, status, related-object lookups, and free-text search.
- **Pagination**: `CustomPageNumberPagination` (`page_size=10`, `page_size_query_param='page_size'`, `max_page_size=10000`).
- **Permissions & Validation**:
  - `has_project_permission(request_method, user, project, solution_name)` enforces owner/team/admin/RW checks.
  - Utility filters for Azure connections (`has_accepted_internal_user_connection`), currency formatting, PDF invoice generation, and SharePoint folder recursion (`build_structure`).
- **Atomicity**: Actions that create/update multiple related models (contacts, invoices, site reports) are wrapped in `@transaction.atomic`. 
