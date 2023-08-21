from typing import Annotated, Literal, Any
import logging
import contextlib
from functools import lru_cache
import asyncio
import urllib.parse as urlparse
import httpx
import aio_pika
from pydantic import BaseModel, ValidationError
from pydantic.functional_validators import AfterValidator, BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict
from motor.motor_asyncio import AsyncIOMotorClient


logger = logging.getLogger(__name__)


class AmqpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='AMQP_')
    host: str = 'localhost'
    port: int = 5672
    user: str = "guest"
    password: str = "guest"


@lru_cache
def amqp() -> AmqpSettings:
    return AmqpSettings(
        host="localhost"  # FIXME
    )


class DelegateServicesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='SERVICE_HOST_')
    badlisted_words: str
    spacy_nounphrases: str


@lru_cache
def services() -> DelegateServicesSettings:
    return DelegateServicesSettings(
        badlisted_words="localhost:8018",    # FIXME
        spacy_nounphrases="localhost:8006"
    ) 


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='DB_')
    host: str
    port: int


@lru_cache
def mongo() -> MongoSettings:
    return MongoSettings(
        host="localhost"
    )


def validate_delegate_service_name(service_name: str):
    valid_names = services().model_fields_set
    if service_name in valid_names:
        return service_name
    raise ValueError(f"Unknown service '{service_name}', "
                     f"available services: {valid_names}")


Jsonable = dict[str, Any] | list[Any]


class RemoteCallModel(BaseModel):
    method: Annotated[
        Literal["POST"],
        BeforeValidator(str.upper)
    ]
    delegate_service: Annotated[
        str,
        AfterValidator(validate_delegate_service_name)
    ]
    path: str
    body: Jsonable


class ResultModel(BaseModel):
    status_code: int
    body: Jsonable


def strip_message(message: aio_pika.Message) -> dict:
    return {k: v for (k, v) in message.info().items() if v is not None}


async def setup():

    async with contextlib.AsyncExitStack() as exit_stack:
        aenter = exit_stack.enter_async_context
        
        connection = await aio_pika.connect_robust(
            host=amqp().host,
            port=amqp().port,
            login=amqp().user,
            password=amqp().password,
        )
        channel = await connection.channel()
        input_queue = await channel.declare_queue("gateway.input", auto_delete=True)
        output_queue = await channel.declare_queue("gateway.output", auto_delete=True)
        await aenter(connection)

        http_client = httpx.AsyncClient()
        http_session = await aenter(http_client)

        mongo_client = AsyncIOMotorClient(mongo().host, mongo().port)
        mongo_db = mongo_client["my_hardcoded_db_name"]
        mongo_collection = mongo_db["gateway_messages"]
        exit_stack.callback(mongo_client.close)
        
        async def consumer(in_msg: aio_pika.IncomingMessage) -> None:
            async with in_msg.process():
                try:
                    remote_call = RemoteCallModel.model_validate_json(in_msg.body)
                except ValidationError as e:
                    result = ResultModel(status_code=400, body=e.errors())
                else:
                    url = urlparse.urlunsplit([
                        "http",
                        getattr(services(), remote_call.delegate_service),
                        remote_call.path,
                        "", ""
                    ])
                    try:
                        response = await http_session.request(
                            method=remote_call.method,
                            url=url,
                            json=remote_call.body
                        )
                        result = ResultModel(status_code=response.status_code,
                                             body=response.json())
                    except Exception:
                        logger.exception("Exception on request to delegate service %s",
                                         remote_call.delegate_service)
                        return
                
                out_msg = aio_pika.Message(
                    result.model_dump_json().encode(),
                    correlation_id=in_msg.correlation_id,
                )

                try:
                    await channel.default_exchange.publish(
                        out_msg,
                        routing_key=output_queue.name
                    )
                except Exception:
                    logger.exception("Unable to send message to '%s'", output_queue.name)
                    return
                
                await mongo_collection.insert_many([
                    strip_message(in_msg),
                    strip_message(out_msg)
                ])
                
        await input_queue.consume(consumer)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(setup())
    except Exception:
        logger.fatal("Unhandled exception", exc_info=True)
        raise
