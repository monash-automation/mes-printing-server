from pathlib import Path
from types import TracebackType

from aiohttp import ClientSession
from mes_opcua_server.models import Printer as OpcuaPrinter
from opcuax import OpcuaClient
from opcuax.model import TBaseModel, TOpcuaModel
from pydantic_core import Url

from db import Database
from db.models import Printer
from printer import ActualPrinter, MockPrinter, OctoPrinter, PrinterApi, PrusaPrinter
from setting import AppSettings, EnvAppSettings
from worker import PrinterWorker, Scheduler


class MockOpcuaClient(OpcuaClient):
    async def refresh(self, model: TBaseModel) -> None:
        return

    async def update(self, name: str, model: TOpcuaModel) -> TOpcuaModel:
        return model

    async def commit(self) -> None:
        while not self.update_tasks.empty():
            self.update_tasks.get_nowait()

    async def get_object(
        self, model_class: type[TOpcuaModel], name: str
    ) -> TOpcuaModel:
        return model_class()

    async def __aenter__(self):
        pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def _opcua_client(url: Url, namespace: str) -> OpcuaClient:
    if url.host and "mock" in url.host:
        return MockOpcuaClient(str(url), namespace)
    else:
        return OpcuaClient(str(url), namespace)


class AppContext:
    settings: AppSettings
    database: Database
    upload_path: Path
    opcua_client: OpcuaClient
    http_session: ClientSession
    workers: dict[int, PrinterWorker]
    scheduler: Scheduler

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url)
        self.upload_path = settings.upload_path
        self.opcua_client = _opcua_client(
            settings.opcua_server_url, settings.opcua_server_namespace
        )
        self.workers = {}
        self.scheduler = Scheduler(
            session=self.database.new_session(), auto_assign=settings.auto_schedule
        )

    @staticmethod
    def from_env() -> "AppContext":
        return AppContext(settings=EnvAppSettings())

    def actual_printer(self, printer: Printer) -> ActualPrinter:
        match printer.api:
            case PrinterApi.OctoPrint:
                return OctoPrinter(
                    url=printer.url, api_key=printer.api_key, session=self.http_session
                )
            case PrinterApi.PrusaLink:
                return PrusaPrinter(
                    url=printer.url, api_key=printer.api_key, session=self.http_session
                )
            case PrinterApi.Mock:
                return MockPrinter(
                    url=printer.url,
                    api_key=printer.api_key,
                    interval=self.settings.mock_printer_interval,
                    job_time=self.settings.mock_printer_job_time,
                )
            case _:
                raise NotImplementedError

    async def printer_worker(self, printer: Printer) -> PrinterWorker:
        assert printer.id is not None

        if printer.id in self.workers:
            return self.workers[printer.id]

        virtual_printer = await self.opcua_client.get_object(
            OpcuaPrinter, printer.opcua_name
        )
        actual_printer: ActualPrinter = self.actual_printer(printer)
        worker = PrinterWorker(
            session=self.database.new_session(),
            printer_id=printer.id,
            opcua_name=printer.opcua_name,
            model=printer,
            opcua_printer=virtual_printer,
            actual_printer=actual_printer,
            interval=self.settings.printer_worker_interval,
            opcua_client=self.opcua_client,
            scheduler=self.scheduler,
        )
        assert printer.id is not None

        self.workers[printer.id] = worker
        return worker

    async def start_printer_worker(self, printer: Printer) -> None:
        worker = await self.printer_worker(printer)
        worker.start()

    async def stop_printer_worker(self, printer: Printer) -> None:
        worker = await self.printer_worker(printer)
        worker.stop()
        del self.workers[printer.id]

    async def start_printer_workers(self) -> None:
        async with self.database.new_session() as session:
            printers = await session.active_printers()

            for printer in printers:
                worker = await self.printer_worker(printer)
                worker.start()

    async def __aenter__(self) -> "AppContext":
        # ClientSession must be inited in an Awaitable
        self.http_session = ClientSession()
        await self.opcua_client.__aenter__()
        await self.database.create_db_and_tables()
        self.scheduler.run()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.scheduler.session.close()
        self.scheduler.stop()
        await self.opcua_client.__aexit__(exc_type, exc_val, exc_tb)
        await self.http_session.close()
        await self.database.close()

        for worker in self.workers.values():
            worker.stop()
