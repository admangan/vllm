import argparse
import json
from typing import AsyncGenerator

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn
import ray
from ray import serve

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.sampling_params import SamplingParams
from vllm.utils import random_uuid

TIMEOUT_KEEP_ALIVE = 5  # seconds.
TIMEOUT_TO_PREVENT_DEADLOCK = 1  # seconds.
app = FastAPI()
    

@serve.deployment(num_replicas=2, ray_actor_options={"num_gpus": 2}, route_prefix="/")
@serve.ingress(app)
class LanguageModel:
    def __init__(self, args, engine_args, engine):
        self.engine_args = engine_args
        self.engine = engine

    @app.post("/generate")
    async def generate(self, request):
        """Generate completion for the request.

        The request should be a JSON object with the following fields:
        - prompt: the prompt to use for the generation.
        - stream: whether to stream the results or not.
        - other fields: the sampling parameters (See `SamplingParams` for details).
        """
        request_dict = await request.json()
        prompt = request_dict.pop("prompt")
        stream = request_dict.pop("stream", False)
        sampling_params = SamplingParams(**request_dict)
        request_id = random_uuid()
        results_generator = self.engine.generate(prompt, sampling_params, request_id)

        # Streaming case
        async def stream_results() -> AsyncGenerator[bytes, None]:
            async for request_output in results_generator:
                prompt = request_output.prompt
                text_outputs = [
                    prompt + output.text for output in request_output.outputs
                ]
                ret = {"text": text_outputs}
                yield (json.dumps(ret) + "\0").encode("utf-8")

        async def abort_request() -> None:
            await self.engine.abort(request_id)

        if stream:
            background_tasks = BackgroundTasks()
            # Abort the request if the client disconnects.
            background_tasks.add_task(abort_request)
            return StreamingResponse(stream_results(), background=background_tasks)

        # Non-streaming case
        final_output = None
        async for request_output in results_generator:
            if await request.is_disconnected():
                # Abort the request if the client disconnects.
                await self.engine.abort(request_id)
                return Response(status_code=499)
            final_output = request_output

        assert final_output is not None
        prompt = final_output.prompt
        text_outputs = [prompt + output.text for output in final_output.outputs]
        ret = {"text": text_outputs}
        return JSONResponse(ret)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser = AsyncEngineArgs.add_cli_args(parser)
    args = parser.parse_args()

    engine_args = AsyncEngineArgs.from_cli_args(args)
    engine = AsyncLLMEngine.from_engine_args(engine_args)
    
    serve.run(LanguageModel.bind(args, engine_args, engine))
    # Create a Ray Serve instance.
    

    print("Serving on http://{}:{}".format(args.host, args.port))

    # engine_args = AsyncEngineArgs.from_cli_args(args)
    # engine = AsyncLLMEngine.from_engine_args(engine_args)

    # uvicorn.run(app,
    #             host=args.host,
    #             port=args.port,
    #             log_level="debug",
    #             timeout_keep_alive=TIMEOUT_KEEP_ALIVE)
