
import json
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ENV = os.getenv("VIKTOR_ENV", "beta")
WORKSPACE_ID = int(os.getenv("VIKTOR_WORKSPACE_ID", "2539"))
ENTITY_ID = int(os.getenv("VIKTOR_ENTITY_ID", "2262"))
VIKTOR_TOKEN = os.getenv("VIKTOR_TOKEN")
MAX_POLL_SECONDS = int(os.getenv("VIKTOR_MAX_POLL_SECONDS", "60"))

API_BASE = f"https://{ENV}.viktor.ai/api"
JOB_URL = f"{API_BASE}/workspaces/{WORKSPACE_ID}/entities/{ENTITY_ID}/jobs/"
FILES_URL = f"{API_BASE}/workspaces/{WORKSPACE_ID}/files/"
ENTITY_URL = f"{API_BASE}/workspaces/{WORKSPACE_ID}/entities/{ENTITY_ID}"

AUTH_HEADERS = {"Authorization": f"Bearer {VIKTOR_TOKEN}"}
JSON_HEADERS = {**AUTH_HEADERS, "Content-Type": "application/json"}

def parse_json_response(response: requests.Response, context: str) -> dict:
    """Return JSON body or raise a descriptive error when the payload isn't JSON."""
    try:
        return response.json()
    except requests.JSONDecodeError as exc:
        snippet = response.text[:500]
        raise RuntimeError(
            f"{context} returned non-JSON (status={response.status_code}): {snippet}"
        ) from exc


def mark_file_uploaded(file_id: int) -> None:
    """Notify the API that the file was uploaded to S3."""
    payload = {"scope": "entity", "entity_id": ENTITY_ID}
    url = f"{FILES_URL}{file_id}/uploaded/"
    response = requests.post(url, headers=JSON_HEADERS, json=payload)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Failed to mark file as uploaded (status={response.status_code}): {response.text}"
        )
    logger.info("Marked file %s as uploaded", file_id)


def upload_json_file(json_data: dict, filename: str = "input_data.json") -> int:
    """Create a file via the REST API and upload JSON content, returning the file id."""
    create_payload = {
        "scope": "entity",
        "entity_id": ENTITY_ID,
        "filename": filename,
    }
    response = requests.post(FILES_URL, headers=JSON_HEADERS, json=create_payload)
    if response.status_code != 201:
        raise RuntimeError(
            f"Failed to create file (status={response.status_code}): {response.text}"
        )

    file_meta = parse_json_response(response, "Create file response")
    upload_url = file_meta.get("temp_upload_url")
    upload_data = file_meta.get("temp_upload_data")
    if not upload_url or not upload_data:
        raise RuntimeError(f"Create file response missing upload data: {file_meta}")

    json_content = json.dumps(json_data, indent=2)
    files = {"file": (filename, json_content, "application/json")}
    upload_response = requests.post(upload_url, data=upload_data, files=files)
    if upload_response.status_code != 204:
        raise RuntimeError(
            f"File upload failed (status={upload_response.status_code}): {upload_response.text}"
        )

    logger.info("Uploaded file data to S3")
    file_id = file_meta["id"]
    mark_file_uploaded(file_id)
    return file_id


def update_entity_params(file_resource_id: int) -> None:
    """Update the entity parameters to persist the uploaded file in the UI"""
    logger.info("Updating entity parameters to persist the file in UI...")
    
    payload = {
        "params": {
            "input_file": {
                "file_id": file_resource_id
            }
        }
    }
    
    response = requests.patch(ENTITY_URL, headers=JSON_HEADERS, json=payload)
    
    if response.status_code != 200:
        logger.error(
            "Failed to update entity (status=%s): %s",
            response.status_code,
            response.text,
        )
        return
    
    logger.info("✅ Entity parameters updated! File should now appear in the UI FileField.")


def poll_job(job_url: str) -> dict:
    """Poll the job endpoint until it completes"""
    deadline = time.time() + MAX_POLL_SECONDS
    while time.time() < deadline:
        res = requests.get(job_url, headers=AUTH_HEADERS)
        body = parse_json_response(res, "Job polling response")
        status = body.get("status")
        
        if status == "success":
            logger.info("Job completed successfully!")
            return body.get("content", {})
        
        if status in ("error", "error_user"):
            raise RuntimeError(f"Job failed: {body.get('error_message')}")
        
        logger.info(f"Job status: {status}, polling again...")
        time.sleep(1)

    raise TimeoutError(f"Job did not finish within {MAX_POLL_SECONDS} seconds")


def main() -> None:
    if not VIKTOR_TOKEN:
        logger.error("Missing VIKTOR_TOKEN")
        sys.exit(1)

    # Create JSON input data
    input_data = {
        "number_1": 15,
        "number_2": 7
    }
    
    logger.info(f"Uploading JSON file with data: {input_data}")
    
    # Step 1: Upload the JSON file
    file_resource_id = upload_json_file(input_data)
    logger.info(f"File uploaded with resource ID: {file_resource_id}")
    
    # Step 2: Update entity parameters so file appears in UI FileField
    update_entity_params(file_resource_id)
    
    # Step 3: Call the compute_results method with the uploaded file
    payload = {
        "method_name": "compute_results",
        "params": {
            "input_file": {
                "file_id": file_resource_id
            }
        },
        "poll_result": True,
    }
    
    logger.info("Creating job...")
    response = requests.post(url=JOB_URL, headers=JSON_HEADERS, json=payload)
    
    if response.status_code != 200:
        logger.error(
            "API call failed (status=%s): %s",
            response.status_code,
            response.text,
        )
        sys.exit(1)
    
    job_data = parse_json_response(response, "Job creation")
    job_url = job_data.get("url")
    status = job_data.get("status")
    kind = job_data.get("kind")
    
    if job_url:
        logger.info(f"Job created: {job_url}")
        result_content = poll_job(job_url)
    elif status == "success" and kind == "result":
        logger.info("Job completed synchronously")
        result_content = job_data.get("content", {})
    else:
        logger.error("Unexpected job response: %s", job_data)
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("DOWNLOAD URL FOR RESULTS:")
    logger.info(json.dumps(result_content, indent=2))
    logger.info("=" * 50)
    
    # Step 4: Download and display the actual computation results
    if "url" in result_content:
        download_url = result_content["url"]
        logger.info("Downloading computation results...")
        
        download_response = requests.get(download_url)
        if download_response.status_code == 200:
            computation_results = download_response.json()
            logger.info("=" * 50)
            logger.info("COMPUTATION RESULTS:")
            logger.info(json.dumps(computation_results, indent=2))
            logger.info("=" * 50)
        else:
            logger.error(f"Failed to download results (status={download_response.status_code})")


if __name__ == "__main__":
    main()