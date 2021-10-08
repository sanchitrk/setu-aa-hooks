import datetime
import json

import requests
from flask import Flask, jsonify, request
from google.cloud import pubsub_v1, tasks_v2
from google.protobuf import timestamp_pb2
from pymongo import MongoClient

from .config import MONGODB_HOST, MONGODB_NAME, MONGODB_PWD, MONGODB_USER, SETU_PEACEMAKER_BASE_URL

publisher = pubsub_v1.PublisherClient()
task_client = tasks_v2.CloudTasksClient()


PROJECT_ID = "serengeti-development"
AA_FI_READY_TOPIC = "pub-aa-fi-ready"

LOCATION_ID = "asia-south1"

QUEUE = "aa-fiready-queue"

MONGO_URL = f"mongodb+srv://{MONGODB_USER}:{MONGODB_PWD}@{MONGODB_HOST}/{MONGODB_NAME}?retryWrites=true&w=majority"

# see https://github.com/dcrosta/flask-pymongo/issues/87
mongodb = MongoClient(MONGO_URL, connect=False)[MONGODB_NAME]

app = Flask(__name__)

URL_STEP_2 = f"{SETU_PEACEMAKER_BASE_URL}/-/2"
URL_STEP_3 = f"{SETU_PEACEMAKER_BASE_URL}/-/3"
URL_STEP_4 = f"{SETU_PEACEMAKER_BASE_URL}/-/4"


@app.route("/")
def ok():
    return "ok, all good!"


@app.route("/Consent/Notification", methods=["POST"])
def consent_notification():
    """
    After we get notification from the SETU POST webhook, at this stage we
    get the consent id which we store in the workflow item db, to main workflow state
    Trigger next step of the AA workflow identified by the workflow id in our database.
    """
    data = request.get_json(force=True)
    print("--- received data ---")
    print(data)
    print("---------------------")
    consent_status_notification = data["ConsentStatusNotification"]
    consent_status = consent_status_notification["consentStatus"]
    print(f"solving for happy cases the consent status is {consent_status}")

    consent_id = consent_status_notification["consentId"]  # use this to get a signed consent
    consent_handle = consent_status_notification[
        "consentHandle"
    ]  # use this fot get the workflow item
    #
    # make database connection to get the workflow item for the received consent handle
    projection = {"_id": False, "userRef": True, "workflowId": True}
    workflow_item_doc = mongodb.get_collection("aaSetuWorkflows").find_one(
        {"consentFlow.consentHandle": consent_handle}, projection=projection
    )
    if not workflow_item_doc:
        print("oops this should not happen!")
        raise ValueError("consent handle or workflow item does not exists.")
    #
    # save the consent response from SETU notification in store - required later for next states
    workflow_id = workflow_item_doc["workflowId"]
    user_ref = workflow_item_doc["userRef"]
    print(f"update workflow with consent id for workflowId {workflow_id} and userRef {user_ref}")

    update_fields = {"consentFlow.consentId": consent_id}
    result = mongodb.get_collection("aaSetuWorkflows").update_one(
        {"workflowId": workflow_id, "userRef": user_ref},
        {"$set": update_fields},
    )
    print(f"updated collection aaSetuWorkflows matched count: {result.matched_count}")
    #
    #
    payload = {"workflow_id": workflow_id}
    payload = json.dumps(payload)
    response = requests.request("POST", URL_STEP_2, data=payload)
    print(response.text)
    response = requests.request("POST", URL_STEP_3, data=payload)
    print(response.text)
    response = requests.request("POST", URL_STEP_4, data=payload)
    print(response.text)
    #
    #
    url = f"{SETU_PEACEMAKER_BASE_URL}/taskhandler"
    task_http_payload = {"workflow_id": workflow_id}
    in_seconds = 180

    # if task_name is None - auto generated
    # keeping task name as workflow id, also once invoked cannot use the same task_name
    task_name = workflow_id
    parent = task_client.queue_path(PROJECT_ID, LOCATION_ID, QUEUE)
    task = {
        "http_request": {  # Specify the type of request.
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,  # The full url path that the task will be sent to.
        }
    }
    task["http_request"]["headers"] = {"Content-type": "application/json"}
    converted_payload = task_http_payload.encode()
    task["http_request"]["body"] = converted_payload

    if in_seconds is not None:
        # Convert "seconds from now" into an rfc3339 datetime string.
        d = datetime.datetime.utcnow() + datetime.timedelta(seconds=in_seconds)

        # Create Timestamp protobuf.
        timestamp = timestamp_pb2.Timestamp()
        timestamp.FromDatetime(d)

        # Add the timestamp to the tasks.
        task["schedule_time"] = timestamp

    if task_name is not None:
        # Add the name to tasks.
        task["name"] = task_client.task_path(PROJECT_ID, LOCATION_ID, QUEUE, task_name)

    response = task_client.create_task(request={"parent": parent, "task": task})
    return jsonify({"workflow_id": workflow_id})


@app.route("/FI/Notification", methods=["POST"])
def fi_notification():
    """
    once the data is prepared by the FIP, it notifies AA which then
    notifies us via this POST webhook

    Note: sometimes we notice that this hook which tells that data is prepared is not triggered by SETU
    perhaps it could be because of the sandbox issue
    """
    data = request.get_json(force=True)
    print("--- received data ---")
    print(data)
    print("---------------------")

    fi_status_notification = data["FIStatusNotification"]
    session_status = fi_status_notification["sessionStatus"]
    print(f"got FI Notification with session status {session_status}")

    session_id = fi_status_notification["sessionId"]
    projection = {"_id": False, "userRef": True, "workflowId": True}
    workflow_item_doc = mongodb.get_collection("aaSetuWorkflows").find_one(
        {"dataFlow.sessionId": session_id}, projection=projection
    )

    if not workflow_item_doc:
        print("oops this should not happen!")
        raise ValueError("consent handle or workflow item does not exists.")

    workflow_id = workflow_item_doc["workflowId"]
    userRef = workflow_item_doc["userRef"]

    print(f"FI notification for workflowId {workflow_id} and userRef {userRef}")
    #
    # attach our internal workflowId identifier which contains the stored states
    pubsub_data = {"workflowId": workflow_id}
    topic_path = publisher.topic_path(PROJECT_ID, AA_FI_READY_TOPIC)
    pubsub_data_json_string = json.dumps(pubsub_data).encode("utf-8")
    future = publisher.publish(topic_path, pubsub_data_json_string)
    print(future.result())
    print(f"Published message to topic {topic_path}.")
    return jsonify({"workflow_id": workflow_id})
