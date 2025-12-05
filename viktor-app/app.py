import json
import os
import tempfile
import uuid
import viktor as vkt
import os
from aps_automation_sdk.classes import  ActivityInputParameter, ActivityOutputParameter, WorkItem 
from aps_automation_sdk.utils import get_token, set_nickname
from model_translation import translate_da_result_for_viewing, get_viewables_from_urn
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Annotated

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")


class APSresult(vkt.WebResult):
    def __init__(self, urn: Annotated[str, "bs64 URN from model derivative"] | None = None):
        token = get_token(CLIENT_ID, CLIENT_SECRET)
        
        # Get viewables from the translated model
        viewables = []
        if urn:
            try:
                viewables = get_viewables_from_urn(urn)
            except Exception as e:
                print(f"Warning: Could not fetch viewables: {e}")
        
        html = (Path(__file__).parent / 'ViewableViewer.html').read_text()
        html = html.replace('APS_TOKEN_PLACEHOLDER', token)
        html = html.replace('URN_PLACEHOLDER', urn)
        html = html.replace('VIEWABLES_PLACEHOLDER', json.dumps(viewables))
        super().__init__(html=html)
        

class APSView(vkt.WebView):
    pass
class Parametrization(vkt.Parametrization):
    input_file = vkt.FileField("Upload JSON File", file_types=[".json"])
class Controller(vkt.Controller):
    parametrization = Parametrization
    
    @APSView("Revit View",duration_guess=40)
    def run_work_item(self, params, **kwargs) -> None:
        if not params.input_file:
            raise RuntimeError("Upload JSON File First!")

        # Get authentication token
        token = get_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
        # Set nickname. If the app already has a nickname, the previous one will be returned.
        nickname = set_nickname(token, "myUniqueNickNameHere")
        print(f"Authentication successful. Nickname: {nickname}")


        activity_name = "CreateBeamElementsActivity"
        alias = "prod"
        bucket_key = uuid.uuid4().hex
        activity_full_alias = f"{nickname}.{activity_name}+{alias}"
        output_object_key="result.rvt"
        input_revit = ActivityInputParameter(
            name="rvtFile",
            localName="input.rvt",
            verb="get",
            description="Input Revit File",
            required=True,
            is_engine_input=True,
            bucketKey=bucket_key,
            objectKey="input.rvt",
        )

        output_file = ActivityOutputParameter(
            name="result",
            localName="result.rvt",
            verb="put",
            description="Output revit file",
            bucketKey=bucket_key,
            objectKey=output_object_key,
        )

        input_json = ActivityInputParameter(
            name="jsonFile",
            localName="structure.json",
            verb="get",
            description="Input Revit File",
            required=True,
            bucketKey=bucket_key,
            objectKey="structure.json",
        )


        # Generate json file
        staad_payload = self.get_json_from_params(params=params)
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as json_temp_file:
            json.dump(staad_payload, json_temp_file)
            json_temp_file.flush()
            temp_path = json_temp_file.name

            input_json.upload_file_to_oss(file_path=temp_path, token=token)

        # Upload Work Item
        input_rvt_path = Path.cwd() / "files" / "revit_input.rvt"
        input_revit.upload_file_to_oss(file_path=str(input_rvt_path), token=token)
        print(f"Input Revit file uploaded: {input_rvt_path.name}")

        work_item = WorkItem(
            parameters=[input_revit, output_file, input_json],
            activity_full_alias=activity_full_alias
        )
        status_resp = work_item.execute(token=token, max_wait=600, interval=10)
        last_status = status_resp.get("status", "")
        print(f"Work item completed with status: {last_status}")

        viewer_urn = translate_da_result_for_viewing(bucket_key, output_object_key)
        return APSresult(urn=viewer_urn)

    def get_json_from_params(self, params) -> dict[str, Any]:
        if params.input_file:
            with params.input_file.file.open() as f:
                json_data = json.load(f)
                return json_data 
            