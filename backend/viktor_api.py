
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_ENV = os.getenv("VIKTOR_ENV", "beta")
DEFAULT_WORKSPACE_ID = int(os.getenv("VIKTOR_WORKSPACE_ID", "2539"))
DEFAULT_ENTITY_ID = int(os.getenv("VIKTOR_ENTITY_ID", "2262"))
DEFAULT_TOKEN = os.getenv("VIKTOR_TOKEN")
DEFAULT_MAX_POLL = int(os.getenv("VIKTOR_MAX_POLL_SECONDS", "60"))


@dataclass
class ViktorConfig:
    scheme: str
    host: str
    workspace_id: int
    entity_id: int
    token: str
    max_poll_seconds: int = DEFAULT_MAX_POLL

    @property
    def api_base(self) -> str:
        return f"{self.scheme}://{self.host}/api"

    @property
    def job_url(self) -> str:
        return f"{self.api_base}/workspaces/{self.workspace_id}/entities/{self.entity_id}/jobs/"

    @property
    def files_url(self) -> str:
        return f"{self.api_base}/workspaces/{self.workspace_id}/files/"

    @property
    def entity_url(self) -> str:
        return f"{self.api_base}/workspaces/{self.workspace_id}/entities/{self.entity_id}"


def extract_int_after(parts: list[str], marker: str) -> int | None:
    for idx, value in enumerate(parts):
        if value == marker and idx + 1 < len(parts):
            try:
                return int(parts[idx + 1])
            except ValueError:
                return None
    return None


def create_config_from_url(viktor_url: str, token: str, max_poll_seconds: int | None = None) -> ViktorConfig:
    """Build a config object from a user-provided editor URL."""

    parsed = urlparse(viktor_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    if not host:
        raise ValueError("Invalid VIKTOR URL; no host present")

    parts = [segment for segment in parsed.path.split("/") if segment]
    workspace_id = extract_int_after(parts, "workspaces")
    if workspace_id is None:
        raise ValueError("VIKTOR URL missing workspace id")

    entity_id: int | None = None
    for marker in ("entities", "editor"):
        entity_id = extract_int_after(parts, marker)
        if entity_id is not None:
            break

    if entity_id is None:
        for segment in reversed(parts):
            if segment.isdigit():
                entity_id = int(segment)
                break

    if entity_id is None:
        raise ValueError("VIKTOR URL missing entity id")

    return ViktorConfig(
        scheme=scheme,
        host=host,
        workspace_id=workspace_id,
        entity_id=entity_id,
        token=token,
        max_poll_seconds=max_poll_seconds or DEFAULT_MAX_POLL,
    )


def create_config_from_env() -> ViktorConfig:
    if not DEFAULT_TOKEN:
        raise RuntimeError("Missing VIKTOR_TOKEN environment variable")

    return ViktorConfig(
        scheme="https",
        host=f"{DEFAULT_ENV}.viktor.ai",
        workspace_id=DEFAULT_WORKSPACE_ID,
        entity_id=DEFAULT_ENTITY_ID,
        token=DEFAULT_TOKEN,
        max_poll_seconds=DEFAULT_MAX_POLL,
    )


class ViktorClient:
    def __init__(self, config: ViktorConfig):
        self.config = config
        self.auth_headers = {"Authorization": f"Bearer {config.token}"}
        self.json_headers = {**self.auth_headers, "Content-Type": "application/json"}

    def mark_file_uploaded(self, file_id: int) -> None:
        payload = {"scope": "entity", "entity_id": self.config.entity_id}
        url = f"{self.config.files_url}{file_id}/uploaded/"
        response = requests.post(url, headers=self.json_headers, json=payload)
        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to mark file as uploaded (status={response.status_code}): {response.text}"
            )
        logger.info("Marked file %s as uploaded", file_id)

    def upload_json_file(self, json_data: dict, filename: str = "input_data.json") -> int:
        create_payload = {"scope": "entity", "entity_id": self.config.entity_id, "filename": filename}
        response = requests.post(self.config.files_url, headers=self.json_headers, json=create_payload)
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
        self.mark_file_uploaded(file_id)
        return file_id

    def update_entity_params(self, file_resource_id: int) -> None:
        logger.info("Updating entity parameters to persist the file in UI...")

        payload = {"params": {"input_file": {"file_id": file_resource_id}}}
        response = requests.patch(self.config.entity_url, headers=self.json_headers, json=payload)

        if response.status_code != 200:
            logger.error(
                "Failed to update entity (status=%s): %s",
                response.status_code,
                response.text,
            )
            return

        logger.info("✅ Entity parameters updated! File should now appear in the UI FileField.")

    def poll_job(self, job_url: str) -> dict:
        deadline = time.time() + self.config.max_poll_seconds
        while time.time() < deadline:
            res = requests.get(job_url, headers=self.auth_headers)
            body = parse_json_response(res, "Job polling response")
            status = body.get("status")

            if status == "success":
                logger.info("Job completed successfully!")
                return body.get("content", {})

            if status in ("error", "error_user"):
                raise RuntimeError(f"Job failed: {body.get('error_message')}")

            logger.info("Job status: %s, polling again...", status)
            time.sleep(1)

        raise TimeoutError(f"Job did not finish within {self.config.max_poll_seconds} seconds")

    def create_job(self, file_resource_id: int, method_name: str = "compute_results") -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = {
            "method_name": method_name,
            "params": {"input_file": {"file_id": file_resource_id}},
            "poll_result": True,
        }

        logger.info("Creating job...")
        response = requests.post(url=self.config.job_url, headers=self.json_headers, json=payload)

        if response.status_code != 200:
            raise RuntimeError(
                f"API call failed (status={response.status_code}): {response.text}"
            )

        job_data = parse_json_response(response, "Job creation")
        job_url = job_data.get("url")
        status = job_data.get("status")
        kind = job_data.get("kind")

        if job_url:
            logger.info("Job created: %s", job_url)
            result_content = self.poll_job(job_url)
        elif status == "success" and kind == "result":
            logger.info("Job completed synchronously")
            result_content = job_data.get("content", {})
        else:
            raise RuntimeError(f"Unexpected job response: {job_data}")

        return job_data, result_content


def parse_json_response(response: requests.Response, context: str) -> dict:
    """Return JSON body or raise a descriptive error when the payload isn't JSON."""
    try:
        return response.json()
    except requests.JSONDecodeError as exc:
        snippet = response.text[:500]
        raise RuntimeError(
            f"{context} returned non-JSON (status={response.status_code}): {snippet}"
        ) from exc


def push_geometry_data_to_viktor(viktor_url: str, token: str, geometry_data: dict) -> dict:
    """Upload STAAD geometry to the provided VIKTOR entity."""

    config = create_config_from_url(viktor_url, token)
    client = ViktorClient(config)

    file_resource_id = client.upload_json_file(geometry_data, filename="staad_geometry.json")
    client.update_entity_params(file_resource_id)
    job_data, result_content = client.create_job(file_resource_id)

    return {
        "file_id": file_resource_id,
        "job": job_data,
        "result": result_content,
    }

def main() -> None:
    try:
        config = create_config_from_env()
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

    client = ViktorClient(config)

    input_data = {
        "number_1": 15,
        "number_2": 7,
    }

    logger.info("Uploading JSON file with data: %s", input_data)
    file_resource_id = client.upload_json_file(input_data)
    logger.info("File uploaded with resource ID: %s", file_resource_id)

    client.update_entity_params(file_resource_id)
    job_data, result_content = client.create_job(file_resource_id)

    logger.info("=" * 50)
    logger.info("DOWNLOAD URL FOR RESULTS:")
    logger.info(json.dumps(result_content, indent=2))
    logger.info("=" * 50)

    download_url = result_content.get("url")
    if download_url:
        logger.info("Downloading computation results...")
        download_response = requests.get(download_url)
        if download_response.status_code == 200:
            computation_results = download_response.json()
            logger.info("=" * 50)
            logger.info("COMPUTATION RESULTS:")
            logger.info(json.dumps(computation_results, indent=2))
            logger.info("=" * 50)
        else:
            logger.error("Failed to download results (status=%s)", download_response.status_code)


if __name__ == "__main__":
    main()
