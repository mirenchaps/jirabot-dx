import json
import os
import logging
import base64
import re
import boto3
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from botocore.exceptions import ClientError
from datetime import datetime, timezone, timedelta

# --- CONFIGURATION ---
SECRET_NAME = os.environ.get("SECRETS_MANAGER_KEY")
TABLE_NAME = "JiraBotNotifier"
REGION_NAME = os.environ.get("AWS_REGION", "us-east-1")

PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "") 
DEFAULT_DRIVER_EMAIL = "" #omitted for security

# Compile regex patterns once
CR_PATTERN = re.compile(r"\bCHG\d+\b", re.IGNORECASE)

# Global caches
_jira_headers = None
_circuit_token = None
_token_expiry = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)
secrets_client = boto3.client("secretsmanager", region_name=REGION_NAME)
table = dynamodb.Table(TABLE_NAME)

# --- SECRETS & AUTH ---
def load_secrets():
    try:
        response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        if "SecretString" in response:
            secrets = json.loads(response["SecretString"])
            for key, value in secrets.items():
                os.environ[key] = value
    except ClientError as e:
        logger.error(f"Failed to load secrets: {e}")
        raise

def get_jira_headers():
    global _jira_headers
    if _jira_headers is None:
        user = os.environ.get("JIRA_USER")
        token = os.environ.get("JIRA_API_TOKEN")
        cred_str = f"{user}:{token}"
        encoded = base64.b64encode(cred_str.encode("utf-8")).decode("utf-8")
        _jira_headers = {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}
    return _jira_headers

# --- JIRA API CALLS ---
def get_upcoming_releases(base_url):
    """Fetches unreleased versions happening in the next 2 days."""
    url = f"{base_url}/rest/api/3/project/{PROJECT_KEY}/versions"
    
    req = urllib.request.Request(url, headers=get_jira_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            versions = json.loads(response.read().decode('utf-8'))
            
            upcoming = []
            today = datetime.now(timezone.utc).date()
            
            for v in versions:
                if v.get("released") is False and "releaseDate" in v:
                    r_date = datetime.strptime(v["releaseDate"], "%Y-%m-%d").date()
                    days_until = (r_date - today).days
                    
                    # T-Minus 2 Days logic (0 = today, 2 = 2 days from now)
                    if 0 <= days_until <= 2:
                        v['days_until'] = days_until
                        upcoming.append(v)
            return upcoming
    except Exception as e:
        logger.error(f"Failed to fetch versions: {e}")
        return []

def get_issues_for_release(release_name):
    """Fetches all issues attached to a specific release."""
    # Grab the base_url directly from the environment variables here!
    base_url = os.environ.get("JIRA_URL")
    
    jql = f'project = "{PROJECT_KEY}" AND fixVersion = "{release_name}" ORDER BY created DESC'
    params = {
        "jql": jql,
        "maxResults": 50,
        "fields": "summary,description,issuetype,customfield_10035,comment"
    }    
    
    query_string = urllib.parse.urlencode(params)
    url = f"{base_url}/rest/api/3/search/jql?{query_string}"
    
    req = urllib.request.Request(url, headers=get_jira_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data.get("issues", [])
    except Exception as e:
        logger.error(f"Failed to fetch issues for release {release_name}: {e}")
        return []

def get_related_work_cr(version_id):
    """Step 1 of Waterfall: Check Jira 'Related Work' for CR."""
    base_url = os.environ.get("JIRA_URL")
    url = f"{base_url}/rest/api/3/version/{version_id}/relatedwork"
    
    req = urllib.request.Request(url, headers=get_jira_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            related_work = json.loads(response.read().decode('utf-8'))
            for item in related_work:
                title = item.get("title", "")
                cr_match = re.search(r"\bCHG\d+\b", title, re.IGNORECASE)
                if cr_match:
                    return cr_match.group(0).upper()
    except HTTPError as e:
        pass # 404 means no related work, which is fine
    except Exception:
        pass
    return None

# --- AI SCAVENGER HUNT ---
def get_circuit_token():
    global _circuit_token, _token_expiry
    
    # Return cached token if still valid (expires in 1 hour, cache for 50 minutes)
    if _circuit_token and _token_expiry and datetime.now() < _token_expiry:
        return _circuit_token
        
    client_id = os.environ.get("CIRCUIT_CLIENT_ID")
    client_secret = os.environ.get("CIRCUIT_CLIENT_SECRET")
    auth_str = f"{client_id}:{client_secret}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {b64_auth}"}
    
    req = urllib.request.Request("", data=b"grant_type=client_credentials", headers=headers) #omitted for security
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            token_data = json.loads(response.read().decode('utf-8'))
            _circuit_token = token_data.get("access_token")
            _token_expiry = datetime.now() + timedelta(minutes=50)
            return _circuit_token
    except Exception:
        return None


    
def call_circuit_ai_for_summaries(issues_list):
    """Asks AI to rewrite all ticket summaries in one batch call."""
    app_key = os.environ.get("CIRCUIT_APP_KEY")
    token = get_circuit_token()
    if not token or not app_key or not issues_list: return {}

    url = "" #omitted for security
    headers = {"Content-Type": "application/json", "api-key": token}
    
    # Build the payload of all tickets
    ticket_data = ""
    for issue in issues_list:
        key = issue.get("key")
        summary = issue.get("fields", {}).get("summary", "")
        # Truncate description to save tokens, we only need the gist
        desc = adf_to_text(issue.get("fields", {}).get("description", ""))[:500] 
        ticket_data += f"Ticket: {key}\nOriginal Summary: {summary}\nDescription: {desc}\n---\n"

    prompt = f"""
    You are a Technical Writer helping translate Jira tickets for executive visibility.
    Your job is to rewrite each ticket summary to be clear and understandable to Directors and Managers while preserving ALL important technical details.

    Guidelines:
    - Keep summaries under 100 characters
    - Focus on business impact and technical action
    - DO NOT remove specific technical terms or qualifiers (like "records", "files", "services", etc.)
    - Preserve technical precision - if it says "Virtual Hardware records" keep "records"
    - Use action verbs (Deploy, Fix, Update, Migrate, etc.) but don't oversimplify
    - Only remove unnecessary words like "Code Refactoring:" or workflow prefixes
    - Include key context from the description when helpful

    Ticket Data:
    {ticket_data}

    Return ONLY a valid JSON dictionary where the key is the Ticket ID and the value is the rewritten summary.

    Examples of good rewrites (preserve technical terms):
    - "Code Refactoring: Cancel Unapproved Removable Media Exception Requests" → "Auto-cancel unused removable media exception requests after 10 days"
    - "Prevent duplicate Request Line values in Cloud PC Virtual Hardware records" → "Fix duplicate Request Line values in Cloud PC Virtual Hardware records"
    - "Update user authentication flow for SSO integration" → "Deploy SSO authentication flow updates for improved login"

    Examples of bad rewrites (too aggressive):
    - "Virtual Hardware records" → "Virtual Hardware" (Lost important qualifier)
    - "removable media requests" → "media requests" (Lost specificity)

    Example: {{"EUC-123": "Migrate Cloud PC cleanup jobs to Temporal", "EUC-124": "Fix Linux cert import bug"}}
    """
    payload = json.dumps({
        "messages": [{"role": "system", "content": "Output ONLY valid JSON."}, {"role": "user", "content": prompt}],
        "temperature": 0.2, "max_tokens": 1000, "user": json.dumps({"appkey": app_key})
    }).encode('utf-8')
    
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers), timeout=45)
        data = json.loads(resp.read().decode('utf-8'))
        content = data["choices"][0]["message"]["content"].strip()
        cleaned = content.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"AI Summary Batch failed: {e}")
        return {}

def adf_to_text(adf):
    """Converts Jira ADF description to plain text efficiently."""
    if not adf: return ""
    if isinstance(adf, str): return adf
    
    # Use list comprehension for better performance
    if isinstance(adf, list): 
        return "\n".join(text for item in adf if (text := adf_to_text(item)))
    
    if isinstance(adf, dict):
        if adf.get("type") == "text": 
            return adf.get("text", "")
        if adf.get("type") == "hardBreak": 
            return "\n"
        content = adf.get("content", [])
        if isinstance(content, list): 
            return " ".join(text for item in content if (text := adf_to_text(item)))
    
    return str(adf)

# --- DYNAMODB ---
def get_db_release(release_id):
    try:
        resp = table.get_item(Key={"ticket_id": f"release-{release_id}"})
        return resp.get("Item")
    except ClientError:
        return None

def save_individual_work_items(release_id, release_name, work_items, deployment_date, team_name="Unknown"):
    """Save each work item as a separate DynamoDB record for tracking using existing schema."""
    if not work_items:
        return
    
    try:
        with table.batch_writer() as batch:
            for item in work_items:
                jira_base_url = os.environ.get("JIRA_URL", "") #omitted for security
                
                # Map to existing DynamoDB schema - matching current ticket records
                work_item_record = {
                    "ticket_id": item["ticket_id"],
                    "project": item.get("summary", "N/A"),
                    "status": "sent",
                    "deployment_date": deployment_date,
                    "cr_waived": item.get("cr") == "N/A",
                    "assignee_email": "",
                    "first_seen_at": item.get("processed_at", datetime.now(timezone.utc).isoformat()),
                    "jira_status": item.get("issue_type", "Unknown"),
                    "reminder_count": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "change_request": item.get("cr", "N/A"),
                    "release_name": release_name,
                    "team": team_name,
                    "deployment_type": "release",
                    "jira_url": f"{jira_base_url}/browse/{item['ticket_id']}"
                }
                batch.put_item(Item=work_item_record)
        
        logger.info(f"Saved {len(work_items)} individual work items for release {release_name}")
    except ClientError as e:
        logger.error(f"Failed to save individual work items for {release_name}: {e}")

def save_db_release(release_id, status, release_name, reminder_count=0, work_items=None, cr_waived=False, deployment_date="N/A"):
    if work_items is None: work_items = []
    try:
        table.put_item(Item={
            "ticket_id": f"release-{release_id}",
            "status": status,
            "project": release_name,
            "reminder_count": reminder_count,
            "work_items": work_items,
            "work_items_count": len(work_items),
            "cr_waived": cr_waived,
            "deployment_date": deployment_date,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Saved release {release_name} to DynamoDB with {len(work_items)} work items")
    except ClientError as e:
        logger.error(f"DB Write Error for {release_name}: {e}")

# --- WEBEX ---
def send_webex_dm(email, text=None, card_content=None):
    token = os.environ.get("WEBEX_ACCESS_TOKEN")
    url = "https://webexapis.com/v1/messages"
    
    payload = {"toPersonEmail": email}
    if text: payload["markdown"] = text
    if card_content: payload["attachments"] = [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card_content}]
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False

def send_webex_card(card_content):
    token = os.environ.get("WEBEX_ACCESS_TOKEN")
    #room_id = "Y2lzY29zcGFyazovL3VzL1JPT00vYjcxOTY5YzAtMjc3Yy0xMWYxLTgxZjUtYzk1MmU5MzI1NzM1"
    room_id = os.environ.get("WEBEX_ROOM_ID")

    url = "https://webexapis.com/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = json.dumps({
        "roomId": room_id, 
        "markdown": "New Release Notification", 
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card_content}]
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error(f"Webex Send Failed: {e}")
        return False

def extract_team_name(issues, release_name):
    """Extract team name from issues efficiently."""
    # Try to get team name from first issue with the field
    for issue in issues:
        agile_team_raw = issue.get("fields", {}).get("customfield_10035")
        if agile_team_raw:
            if isinstance(agile_team_raw, dict):
                team_name = agile_team_raw.get("value") or agile_team_raw.get("name")
            else:
                team_name = str(agile_team_raw)
            if team_name:
                return team_name
    
    # Fallback to release name patterns
    if release_name.startswith("DA"): 
        return "Device Automation"
    elif release_name.startswith("MB"): 
        return "Mobile@Cisco"
    elif release_name.startswith(("W2", "Win")): 
        return "Windows@Cisco"
    elif release_name.startswith("DSI"): 
        return "DSI Team"
    else: 
        return PROJECT_KEY

# --- MAIN HANDLER ---
def lambda_handler(event, context):
    logger.info("Starting Release Poller...")
    load_secrets()

    jira_base_url = os.environ.get("JIRA_URL")
    upcoming_releases = get_upcoming_releases(jira_base_url)
    
    if not upcoming_releases:
        logger.info("No upcoming releases found in the next 2 days.")
        return {"statusCode": 200, "body": "No upcoming releases in the next 2 days."}

    logger.info(f"Found {len(upcoming_releases)} upcoming release(s) to evaluate.")
    sent_count = 0

    for release in upcoming_releases:
        r_id = release.get("id")
        r_name = release.get("name")
        r_date = release.get("releaseDate")
        r_desc = release.get("description", "")
        days_until = release.get("days_until")
        
# 1. Check DB State
        db_item = get_db_release(r_id)
        if db_item and db_item.get("status") == "sent":
            logger.info(f"SKIPPING: '{r_name}' - Already marked as 'sent' in DynamoDB.")
            continue 
            
        cr_waived = db_item.get("cr_waived", False) if db_item else False
            
        logger.info(f"PROCESSING: '{r_name}' (T-Minus {days_until} days)")

        # 2. Fetch Issues
        issues = get_issues_for_release(r_name)
        if not issues:
            logger.info(f"⚠️ SKIPPING: '{r_name}' has no issues attached to it.")
            continue

        # 2.5 EXTRACT TEAM NAME FROM JIRA ONLY
        team_name = "Unknown"  # Default if not found in Jira
        for issue in issues:
            agile_team_raw = issue.get("fields", {}).get("customfield_10035")
            if agile_team_raw:
                if isinstance(agile_team_raw, dict):
                    team_name = agile_team_raw.get("value") or agile_team_raw.get("name") or "Unknown"
                else:
                    team_name = str(agile_team_raw) or "Unknown"
                break 

        # 3. FIND MASTER CR (Release Level)
        master_cr = get_related_work_cr(r_id)
        if not master_cr and r_desc:
            # Try regex pattern matching
            cr_match = re.search(r"\bCHG\d+\b", r_desc, re.IGNORECASE)
            if cr_match: 
                master_cr = cr_match.group(0).upper()
        
        if master_cr:
            logger.info(f"Found Master CR for Release: {master_cr}")

        #4. PROCESS TICKETS, COUNT TYPES & FIND CRS
        cr_groups = {}
        no_cr_group = []
        individual_crs_found = 0
        counts = {"Story": 0, "Bug": 0, "Task": 0}
        
        # Array to store rich data in DynamoDB
        db_work_items = []
        
        logger.info(f"Asking AI to rewrite {len(issues)} ticket summaries...")
        smart_summaries = call_circuit_ai_for_summaries(issues)
        logger.info(f"AI has processed {len(smart_summaries)} summaries successfully")

        for issue in issues:
            key = issue.get("key")
            summary = issue.get("fields", {}).get("summary", "")
            desc = adf_to_text(issue.get("fields", {}).get("description", ""))
            issue_type = issue.get("fields", {}).get("issuetype", {}).get("name", "")
            comments_data = issue.get("fields", {}).get("comment", {}).get("comments", [])
            comments_text = " ".join([adf_to_text(c.get("body", "")) for c in comments_data])
            full_text_to_scan = f"{desc} {comments_text}"

            display_summary = smart_summaries.get(key, summary)
            
            ticket_cr = None
            if not master_cr:
                # Try regex pattern matching
                cr_match = re.search(r"\bCHG\d+\b", full_text_to_scan, re.IGNORECASE)
                if cr_match:
                    ticket_cr = cr_match.group(0).upper()
                    individual_crs_found += 1
                    logger.info(f"Found individual CR [{ticket_cr}] inside work item: {key}")
                    
            # Log this item for DynamoDB - capture comprehensive deployment data
            db_work_items.append({
                "ticket_id": key,
                "summary": summary,
                "description": desc[:500] if desc else "N/A",
                "issue_type": issue_type,
                "cr": ticket_cr if ticket_cr else "N/A",
                "jira_url": f"{jira_base_url}/browse/{key}",
                "processed_at": datetime.now(timezone.utc).isoformat()
            })
                
            icon = "📄"
            if "Story" in issue_type or "Feature" in issue_type: 
                icon = "📗"
                counts["Story"] += 1
            elif "Defect" in issue_type or "Bug" in issue_type: 
                icon = "🐞"
                counts["Bug"] += 1
            elif "Action" in issue_type or "Task" in issue_type: 
                icon = "🔧"
                counts["Task"] += 1
            
            url = f"{jira_base_url}/browse/{key}"

            line = f"{icon} [{key}]({url}) — {display_summary}"
            
            if master_cr:
                no_cr_group.append(line) 
            elif ticket_cr:
                cr_groups.setdefault(ticket_cr, []).append(line)
            else:
                no_cr_group.append(line)
                
        # 5. VALIDATION & ACTION (Send Yes/No Card)
        if not master_cr and individual_crs_found == 0 and not cr_waived:
            reminder_count = db_item.get("reminder_count", 0) if db_item else 0
            driver_email = DEFAULT_DRIVER_EMAIL
            
            logger.warning(f"❌ NO CRs FOUND for '{r_name}'. Sending DM to {driver_email}")
            
            # The new Interactive Card for Releases
            dm_card = {
                "type": "AdaptiveCard",
                "version": "1.3",
                "body": [
                    {"type": "TextBlock", "text": f"⚠️ Missing CR Number: {r_name}", "weight": "Bolder", "size": "Medium", "color": "Warning"},
                    {"type": "TextBlock", "text": f"This release for **{team_name}** is scheduled for deployment in **{days_until} days**, but the bot cannot find a CR Number on the Release OR in any of the attached tickets.\n\nAre you happy to proceed without one?", "wrap": True}
                ],
                "actions": [
                    {
                        "type": "Action.Submit",
                        "title": "✅ Yes, proceed without CR",
                        "data": {
                            "action": "confirm_no_cr", 
                            "ticket_id": f"release-{r_id}",
                            "source_lambda": os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
                        }
                    },
                    {
                        "type": "Action.Submit",
                        "title": "❌ No, I will add the CR",
                        "data": {
                            "action": "reject_no_cr", 
                            "ticket_id": f"release-{r_id}",
                            "source_lambda": os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
                        }
                    },
                    {"type": "Action.OpenUrl", "title": "🔗 View Release in Jira", "url": f"{jira_base_url}/projects/{PROJECT_KEY}/versions/{r_id}"}
                ],
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json"
            }
            
            send_webex_dm(driver_email, text=f"Action Required: {r_name}", card_content=dm_card)
            logger.info(f"Saving {len(db_work_items)} work items to DynamoDB for pending release: {r_name}")
            save_db_release(r_id, "pending_cr", r_name, reminder_count + 1, db_work_items, cr_waived, r_date)
            save_individual_work_items(r_id, r_name, db_work_items, r_date, team_name)
            continue

        # 6. DYNAMICALLY BUILD THE ADAPTIVE CARD
        date_obj = datetime.strptime(r_date, "%Y-%m-%d")
        
        if cr_waived:
            display_cr = "Not required"
        elif master_cr:
            display_cr = master_cr
        else:
            display_cr = "Multiple (Grouped below)"
        
        shipping_parts = []
        if counts["Story"] > 0: shipping_parts.append(f"{counts['Story']} Stories")
        if counts["Bug"] > 0: shipping_parts.append(f"{counts['Bug']} Bugs")
        if counts["Task"] > 0: shipping_parts.append(f"{counts['Task']} Tasks")
        shipping_text = " · ".join(shipping_parts) if shipping_parts else f"{len(issues)} Items"

        visible_blocks = []
        hidden_blocks = []
        ticket_count = 0

        def add_block(text, is_header=False, color="Default"):
            if is_header:
                spacing = "Medium"
            else:
                spacing = "Medium"  # Increased from "Small" to "Medium" for better ticket separation
            block = {"type": "TextBlock", "text": text, "wrap": True, "spacing": spacing}
            if color != "Default": block["color"] = color
            
            if ticket_count < 10:
                visible_blocks.append(block)
            else:
                hidden_blocks.append(block)

        if master_cr or cr_waived:
            for line in no_cr_group:
                add_block(line)
                ticket_count += 1
        else:
            for cr_key, lines in cr_groups.items():
                cr_url = f"https://example.service-now.com/nav_to.do?uri=change_request.do?sysparm_query=number={cr_key}" #omitted for security
                header_text = f"**[{cr_key}]({cr_url})**"
                add_block(header_text, is_header=True)
                
                for line in lines:
                    if ticket_count == 10 and len(hidden_blocks) == 0:
                        hidden_blocks.append({"type": "TextBlock", "text": f"{header_text} *(cont.)*", "wrap": True, "spacing": "Medium"})
                    add_block(line)
                    ticket_count += 1
                    
            if no_cr_group:
                header_text = "**⚠️ No CR Found**"
                add_block(header_text, is_header=True, color="Attention")
                for line in no_cr_group:
                    if ticket_count == 10 and len(hidden_blocks) == 0:
                        hidden_blocks.append({"type": "TextBlock", "text": f"{header_text} *(cont.)*", "wrap": True, "spacing": "Medium", "color": "Attention"})
                    add_block(line)
                    ticket_count += 1

        # --- ASSEMBLE THE JSON ---
        try:
            template_path = "release_card_template.json"
            if os.path.exists(template_path):
                with open(template_path, "r") as f:
                    card_str = f.read()
            else:
                logger.error(f"Template file not found: {template_path}")
                continue
                
            replacements = {
                "{RELEASE_DATE}": date_obj.strftime("%b %d"),
                "{TEAM}": team_name, 
                "{RELEASE_NAME}": r_name,
                "{CR_NUMBER}": display_cr,
                "{SHIPPING_STATS}": shipping_text,
                "{RELEASE_URL}": f"{jira_base_url}/projects/{PROJECT_KEY}/versions/{r_id}"
            }
            
            for k, v in replacements.items():
                safe_val = json.dumps(str(v))[1:-1]
                card_str = card_str.replace(k, safe_val)
                
            card_payload = json.loads(card_str)
            
            card_payload["body"].extend(visible_blocks)

            if hidden_blocks:
                remaining_count = len(issues) - 10
                card_payload["body"].append({
                    "type": "TextBlock",
                    "text": f"+ {remaining_count} more tickets",
                    "isSubtle": True,
                    "size": "Small",
                    "spacing": "Small"
                })
                card_payload["actions"].insert(0, {
                    "type": "Action.ShowCard",
                    "title": "Show All Tickets",
                    "card": {"type": "AdaptiveCard", "body": hidden_blocks}
                })

            card_payload["body"].append({
                "type": "TextBlock",
                "text": "🤖 Powered by AI",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Large"
            })
            
            if master_cr:
                cr_url = f"https://{}/nav_to.do?uri=change_request.do?sysparm_query=number={master_cr}" #omitted for security
                card_payload["actions"].append({
                    "type": "Action.OpenUrl",
                    "title": "Open Change Request",
                    "url": cr_url
                })
                
            if send_webex_card(card_payload):
                logger.info(f"Successfully sent Release Card for '{r_name}' to DX Launch Awareness Room!")
                logger.info(f"Saving {len(db_work_items)} work items to DynamoDB for completed release: {r_name}")
                save_db_release(r_id, "sent", r_name, 0, db_work_items, cr_waived, r_date)
                save_individual_work_items(r_id, r_name, db_work_items, r_date, team_name)
                sent_count += 1
                
        except Exception as e:
            logger.error(f"Failed to build card for {r_name}: {e}")

    return {"statusCode": 200, "body": f"Processed {sent_count} releases."}