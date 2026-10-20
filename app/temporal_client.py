from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.config import Settings


async def connect_client(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=settings.temporal_api_key,
        tls=bool(settings.temporal_api_key),
        data_converter=pydantic_data_converter,
    )
