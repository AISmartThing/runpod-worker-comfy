import runpod
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
import uuid
import logging

from io import BytesIO
from src.client.b2_client import B2Client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 1000
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 600
# Time to wait between poll attempts in milliseconds
COMFY_POLLING_INTERVAL_MS = int(os.environ.get("COMFY_POLLING_INTERVAL_MS", 250))
# Maximum number of poll attempts
COMFY_POLLING_MAX_RETRIES = int(os.environ.get("COMFY_POLLING_MAX_RETRIES", 500))
# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
# see https://docs.runpod.io/docs/handler-additional-controls#refresh-worker
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"
# The path of comfyui
COMFY_ROOT_PATH = "/comfyui"
# b2 related path
B2_INPUT_IMAGE_PATH = "projects/{project_id}/input_{image_name}"
B2_OUTPUT_IMAGE_PATH = "projects/{project_id}/output_{image_name}"
b2_client = B2Client.get_instance()

def generate_project_id():
    return str(uuid.uuid4())

def validate_input(job_input):
    """
    Validates the input for the handler function.

    Args:
        job_input (dict): The input data to validate.

    Returns:
        tuple: A tuple containing the validated data and an error message, if any.
               The structure is (validated_data, error_message).
    """
    # Validate if job_input is provided
    if job_input is None:
        return None, "Please provide input"

    # Check if input is a string and try to parse it as JSON
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    # Validate 'workflow' in input
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    # Validate 'images' in input, if provided
    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "url" in image for image in images
        ):
            return (
                None,
                "'images' must be a list of objects with 'name' and 'image_url' keys",
            )

    # Return validated data and no error
    return {"workflow": workflow, "images": images}, None


def check_server(url, retries=500, delay=50):
    """
    Check if a server is reachable via HTTP GET request

    Args:
    - url (str): The URL to check
    - retries (int, optional): The number of times to attempt connecting to the server. Default is 50
    - delay (int, optional): The time in milliseconds to wait between retries. Default is 500

    Returns:
    bool: True if the server is reachable within the given number of retries, otherwise False
    """

    for i in range(retries):
        try:
            response = requests.get(url)

            # If the response status code is 200, the server is up and running
            if response.status_code == 200:
                logger.info(f"runpod-worker-comfy - API is reachable")
                return
        except requests.RequestException as e:
            # If an exception occurs, the server may not be ready
            pass

        # Wait for the specified delay before retrying
        time.sleep(delay / 1000)

    raise Exception(f"Failed to connect to server at {url} after {retries} attempts.")
    

def upload_images(project_id, images):
    """
    Upload a list of base64 encoded images to the ComfyUI server using the /upload/image endpoint.

    Args:
        images (list): A list of dictionaries, each containing the 'name' of the image and the 'image' as a base64 encoded string.
        server_address (str): The address of the ComfyUI server.

    Returns:
        list: A list of responses from the server for each image upload.
    """
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []

    logger.info("Starting image(s) upload")

    for image in images:
        name = image["name"]
        url = image["url"]

        # Download the image from the URL
        response = requests.get(url)
        blob = response.content
        
        # Detect content type from response headers or fallback to png
        content_type = response.headers.get('content-type', 'image/png')
        
        # Prepare the form data
        files = {
            "image": (name, BytesIO(blob), content_type),
            "overwrite": (None, "true"),
        }

        # POST request to upload the image
        response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files)
        if response.status_code != 200:
            upload_errors.append(f"Error uploading {name}: {response.text}")
        else:
            responses.append(f"Successfully uploaded {name}")

        # upload image to b2
        img_b2_path = B2_INPUT_IMAGE_PATH.format(project_id=project_id, image_name=name)
        b2_client.upload_file(f"{COMFY_ROOT_PATH}/input/{name}", img_b2_path)

    if upload_errors:
        logger.error("Image(s) upload completed with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    logger.info("Image(s) upload complete")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }


def queue_workflow(workflow):
    """
    Queue a workflow to be processed by ComfyUI

    Args:
        workflow (dict): A dictionary containing the workflow to be processed

    Returns:
        dict: The JSON response from ComfyUI after processing the workflow
    """

    # The top level element "prompt" is required by ComfyUI
    data = json.dumps({"prompt": workflow}).encode("utf-8")

    req = urllib.request.Request(f"http://{COMFY_HOST}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    """
    Retrieve the history of a given prompt using its ID

    Args:
        prompt_id (str): The ID of the prompt whose history is to be retrieved

    Returns:
        dict: The history of the prompt, containing all the processing steps and results
    """
    with urllib.request.urlopen(f"http://{COMFY_HOST}/history/{prompt_id}") as response:
        return json.loads(response.read())


def base64_encode(img_path):
    """
    Returns base64 encoded image.

    Args:
        img_path (str): The path to the image

    Returns:
        str: The base64 encoded image
    """
    with open(img_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        return f"{encoded_string}"


def process_output_images(outputs, job_id, project_id):
    """
    This function takes the "outputs" from image generation and the job ID,
    then determines the correct way to return the image, either as a direct URL
    to an AWS S3 bucket or as a base64 encoded string, depending on the
    environment configuration.

    Args:
        outputs (dict): A dictionary containing the outputs from image generation,
                        typically includes node IDs and their respective output data.
        job_id (str): The unique identifier for the job.
        project_id (str): The unique identifier for the project.
    Returns:
        dict: A dictionary with the status ('success' or 'error') and the message,
              which is either the URL to the image in the AWS S3 bucket or a base64
              encoded string of the image. In case of error, the message details the issue.

    The function works as follows:
    - It first determines the output path for the images from an environment variable,
      defaulting to "/comfyui/output" if not set.
    - It then iterates through the outputs to find the filenames of the generated images.
    - After confirming the existence of the image in the output folder, it checks if the
      AWS S3 bucket is configured via the BUCKET_ENDPOINT_URL environment variable.
    - If AWS S3 is configured, it uploads the image to the bucket and returns the URL.
    - If AWS S3 is not configured, it encodes the image in base64 and returns the string.
    - If the image file does not exist in the output folder, it returns an error status
      with a message indicating the missing image file.
    """

    # The path where ComfyUI stores the generated images
    COMFY_OUTPUT_PATH = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")

    output_images = []

    for node_id, node_output in outputs.items():
        if "images" not in node_output:
            continue

        for image in node_output["images"]:
            image_path = os.path.join(image["subfolder"], image["filename"])
            output_images.append(image_path)

    logger.info("Image generation is done")
    logger.info(f"Output images: {output_images}")

    # expected image output folder

    # The image is in the output folder
    result = {
        "status": "success",
        "message": "All images uploaded successfully",
        "output": {
            "images": []
        },
    }
    for image_path in output_images:
        image_name = image_path.split("/")[-1]
        whole_image_path = f"{COMFY_OUTPUT_PATH}/{image_path}"
        if os.path.exists(whole_image_path):
            img_b2_path = B2_OUTPUT_IMAGE_PATH.format(project_id=project_id, image_name=image_name)
            b2_client.upload_file(whole_image_path, img_b2_path)
            b2_url = b2_client.generate_download_url(img_b2_path)
            result["output"]["images"].append({
                "name": image_name,
                "url": b2_url
            })
        else:
            logger.error("The image does not exist in the output folder")
            return {
                "status": "error",
                "message": f"the image does not exist in the specified output folder: {local_image_path}",
            }

    return result


def handler(job):
    """
    The main function that handles a job of generating an image.

    This function validates the input, sends a prompt to ComfyUI for processing,
    polls ComfyUI for result, and retrieves generated images.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status with generated images.
    """
    job_input = job["input"]

    # Make sure that the input is valid
    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    # Extract validated data
    workflow = validated_data["workflow"]
    images = validated_data.get("images")

    # Make sure that the ComfyUI API is available
    check_server(
        f"http://{COMFY_HOST}",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    )

    # generate project_id
    if job_input.get("project_id"):
        project_id = job_input.get("project_id")
    else:
        project_id = generate_project_id()

    # Upload images if they exist
    upload_result = upload_images(project_id, images)

    if upload_result["status"] == "error":
        return upload_result

    # Queue the workflow
    try:
        queued_workflow = queue_workflow(workflow)
        prompt_id = queued_workflow["prompt_id"]
        logger.info(f"Queued workflow with ID {prompt_id}")
    except Exception as e:
        return {"error": f"Error queuing workflow: {str(e)}", "project_id": project_id}

    # Poll for completion
    logger.info("Waiting until image generation is complete")
    retries = 0
    try:
        while retries < COMFY_POLLING_MAX_RETRIES:
            history = get_history(prompt_id)

            # Exit the loop if we have found the history
            if prompt_id in history and history[prompt_id].get("outputs"):
                break
            else:
                # Wait before trying again
                time.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
                retries += 1
        else:
            return {"error": "Max retries reached while waiting for image generation"}
    except Exception as e:
        return {"error": f"Error waiting for image generation: {str(e)}", "project_id": project_id}

    # Get the generated image and return it as URL in an AWS bucket or as base64
    images_result = process_output_images(history[prompt_id].get("outputs"), job["id"], project_id)

    result = {**images_result, "refresh_worker": REFRESH_WORKER, "project_id": project_id}

    return result


# Start the handler only if this script is run directly
if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
