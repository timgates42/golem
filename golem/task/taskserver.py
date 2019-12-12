# pylint: disable=too-many-instance-attributes,too-many-public-methods,
# pylint: disable=too-many-lines

import asyncio
import functools
import itertools
import logging
import os
import shutil
import time
import weakref
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Union,
    Set,
    Tuple,
    TYPE_CHECKING,
)
from dataclasses import dataclass, field

from golem_messages import exceptions as msg_exceptions
from golem_messages import message
from golem_messages.datastructures import tasks as dt_tasks
from golem_messages.datastructures.masking import Mask
from pydispatch import dispatcher
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks, Deferred, \
    TimeoutError as DeferredTimeoutError

import golem.apps
from apps.appsmanager import AppsManager
from apps.core.task.coretask import CoreTask
from golem import constants as gconst
from golem.apps import manager as app_manager
from golem.apps.default import save_built_in_app_definitions
from golem.clientconfigdescriptor import ClientConfigDescriptor
from golem.core.common import short_node_id, deadline_to_timeout, get_log_dir
from golem.core.deferred import (
    asyncio_main_loop,
    deferred_from_future,
    sync_wait,
)
from golem.core.variables import MAX_CONNECT_SOCKET_ADDRESSES
from golem.environments.environment import (
    Environment as OldEnv,
    SupportStatus,
    UnsupportReason,
)
from golem.envs import Environment as NewEnv
from golem.envs.default import (
    register_environments,
    register_built_in_repositories,
)
from golem.marketplace import ProviderPricing
from golem.model import TaskPayment
from golem.network.hyperdrive.client import HyperdriveAsyncClient
from golem.network.transport import msg_queue
from golem.network.transport.network import ProtocolFactory, SessionFactory
from golem.network.transport.tcpnetwork import (
    TCPNetwork, SocketAddress, SafeProtocol)
from golem.network.transport.tcpserver import (
    PendingConnectionsServer,
)
from golem.ranking.helper.trust import Trust
from golem.ranking.manager.database_manager import (
    update_requestor_paid_sum,
    update_requestor_assigned_sum,
    update_requestor_efficiency,
)
from golem.resource.resourcehandshake import ResourceHandshake
from golem.resource.resourcemanager import ResourceManager
from golem.rpc import utils as rpc_utils
from golem.task import helpers as task_helpers
from golem.task import timer
from golem.task.acl import get_acl, setup_acl, AclRule, _DenyAcl as DenyAcl
from golem.task.exceptions import ComputationInProgress
from golem.task.benchmarkmanager import AppBenchmarkManager, BenchmarkManager
from golem.task.envmanager import EnvironmentManager
from golem.task.helpers import calculate_subtask_payment
from golem.task.requestedtaskmanager import RequestedTaskManager
from golem.task.server.whitelist import DockerWhitelistRPC
from golem.task.taskbase import AcceptClientVerdict
from golem.task.taskconnectionshelper import TaskConnectionsHelper
from golem.task.taskstate import TaskOp
from golem.utils import decode_hex
from .server import concent
from .server import helpers
from .server import queue_ as srv_queue
from .server import resources
from .server import verification as srv_verification
from .taskcomputer import TaskComputerAdapter
from .taskkeeper import TaskHeaderKeeper
from .taskmanager import TaskManager
from .tasksession import TaskSession
from golem.cloud.localmanager import LocalContainerManager


if TYPE_CHECKING:
    from golem_messages.datastructures import p2p as dt_p2p  # noqa pylint: disable=unused-import,ungrouped-imports

logger = logging.getLogger(__name__)

tmp_cycler = itertools.cycle(list(range(550)))


class TaskServer(
        PendingConnectionsServer,
        resources.TaskResourcesMixin,
        srv_queue.TaskMessagesQueueMixin,
        srv_verification.VerificationMixin,
        DockerWhitelistRPC,
):

    BENCHMARK_TIMEOUT = 60  # s
    RESULT_SHARE_TIMEOUT = 3600 * 24 * 7 * 2  # s

    # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    def __init__(
            self,
            node,
            config_desc: ClientConfigDescriptor,
            client,
            use_ipv6=False,
            use_docker_manager=True,
            task_archiver=None,
            apps_manager=AppsManager(),
            task_finished_cb=None
    ) -> None:
        DockerWhitelistRPC.__init__(self)

        self.client = client
        self.keys_auth = client.keys_auth
        self.config_desc = config_desc

        Path(self.get_task_computer_root()).mkdir(parents=True, exist_ok=True)

        runtime_logs_dir = get_log_dir(client.datadir)
        new_env_manager = EnvironmentManager(runtime_logs_dir)
        register_built_in_repositories()
        register_environments(
            work_dir=self.get_task_computer_root(),
            env_manager=new_env_manager)

        app_dir = self.get_app_dir()
        built_in_apps = save_built_in_app_definitions(app_dir)

        self.app_manager = app_mgr = app_manager.AppManager()
        for app_def in golem.apps.load_apps_from_dir(app_dir):
            app_mgr.register_app(app_def)
        for app_id in built_in_apps:
            app_mgr.set_enabled(app_id, True)

        self.node = node
        self.task_archiver = task_archiver
        self.task_keeper = TaskHeaderKeeper(
            old_env_manager=client.environments_manager,
            new_env_manager=new_env_manager,
            node=self.node,
            min_price=config_desc.min_price,
            task_archiver=task_archiver)
        self.task_manager = TaskManager(
            self.node,
            self.keys_auth,
            root_path=TaskServer.__get_task_manager_root(client.datadir),
            config_desc=config_desc,
            tasks_dir=os.path.join(client.datadir, 'tasks'),
            apps_manager=apps_manager,
            finished_cb=task_finished_cb,
        )

        self.requested_task_manager = RequestedTaskManager(
            app_manager=app_mgr,
            env_manager=new_env_manager,
            public_key=self.keys_auth.public_key,
            root_path=Path(TaskServer.__get_task_manager_root(client.datadir)),
        )
        self.new_resource_manager = ResourceManager(HyperdriveAsyncClient(
            config_desc.hyperdrive_rpc_port,
            config_desc.hyperdrive_rpc_address,
        ))
        benchmarks = self.task_manager.apps_manager.get_benchmarks()
        self.benchmark_manager = BenchmarkManager(
            node_name=config_desc.node_name,
            task_server=self,
            root_path=self.get_task_computer_root(),
            benchmarks=benchmarks
        )
        self.app_benchmark_manager = AppBenchmarkManager(
            env_manager=new_env_manager,
            root_path=Path(self.get_task_computer_root()),
        )
        self.task_computer = TaskComputerAdapter(
            task_server=self,
            env_manager=new_env_manager,
            use_docker_manager=use_docker_manager,
            finished_cb=task_finished_cb)
        deferred = self._change_task_computer_config(
            config_desc=config_desc,
            run_benchmarks=self.benchmark_manager.benchmarks_needed()
        )
        try:
            sync_wait(deferred, self.BENCHMARK_TIMEOUT)
        except DeferredTimeoutError:
            logger.warning('Benchmark computation timed out')

        if self.config_desc.enable_cloud:
            self.local_container_manager = LocalContainerManager(
                config_desc=config_desc,
                env_manager=new_env_manager,
                root_path=self.get_task_computer_root())
            self.local_container_manager.run_local_containers()

        self.task_connections_helper = TaskConnectionsHelper()
        self.task_connections_helper.task_server = self
        self.sessions: Dict[str, TaskSession] = {}
        self.task_sessions_incoming: weakref.WeakSet = weakref.WeakSet()

        self.max_trust = 1.0
        self.min_trust = 0.0

        self.last_messages: List[Any] = []

        self.results_to_send: Dict[str, Any] = {}
        self.failures_to_send: Dict[str, Any] = {}

        self.use_ipv6 = use_ipv6

        self.forwarded_session_request_timeout = \
            config_desc.waiting_for_task_session_timeout
        self.forwarded_session_requests: Dict[str, Any] = {}
        self.acl = get_acl(
            self.client, max_times=config_desc.disallow_id_max_times)
        self.acl_ip = DenyAcl(
            self.client, max_times=config_desc.disallow_ip_max_times)
        self.resource_handshakes: Dict[str, ResourceHandshake] = {}
        self.requested_tasks: Set[str] = set()
        self._last_task_request_time: float = time.time()

        network = TCPNetwork(
            ProtocolFactory(SafeProtocol, self, SessionFactory(TaskSession)),
            use_ipv6)
        PendingConnectionsServer.__init__(self, config_desc, network)
        srv_queue.TaskMessagesQueueMixin.__init__(self)
        # instantiate ReceivedMessageHandler connected to self
        # to register in golem.network.concent.handlers_library
        from golem.network.concent import \
            received_handler as concent_received_handler
        self.concent_handler = \
            concent_received_handler.TaskServerMessageHandler(self)

        dispatcher.connect(
            self.income_listener,
            signal='golem.income'
        )
        dispatcher.connect(
            self.finished_subtask_listener,
            signal='golem.taskcomputer'
        )
        dispatcher.connect(
            self.finished_task_listener,
            signal='golem.taskmanager'
        )

    def sync_network(self, timeout=None):
        if timeout is None:
            timeout = self.config_desc.task_session_timeout
        jobs = (
            functools.partial(
                super().sync_network,
                timeout=timeout,
            ),
            self._sync_pending,
            self._send_waiting_results,
            self._request_random_task,
            self.task_computer.check_timeout,
            self.task_connections_helper.sync,
            self._sync_forwarded_session_requests,
            self.__remove_old_tasks,
            functools.partial(
                concent.process_messages_received_from_concent,
                concent_service=self.client.concent_service,
            ),
            self.sweep_sessions,
            self.connect_to_nodes,
        )

        for job in jobs:
            try:
                job()
            except Exception:  # pylint: disable=broad-except
                logger.exception("TaskServer.sync_network job %r failed", job)

        if next(tmp_cycler) == 0:
            logger.debug('TASK SERVER TASKS DUMP: %r', self.task_manager.tasks)
            logger.debug('TASK SERVER TASKS STATES: %r',
                         self.task_manager.tasks_states)

    @inlineCallbacks
    def pause(self):
        super().pause()
        yield CoreTask.VERIFICATION_QUEUE.pause()
        self.disconnect()
        self.quit()

    def resume(self):
        super().resume()
        CoreTask.VERIFICATION_QUEUE.resume()

    @inlineCallbacks
    def quit(self):
        self.local_container_manager.quit()
        try:
            future = self.requested_task_manager.stop()
            yield deferred_from_future(asyncio.wait_for(future, timeout=30.))
        except asyncio.TimeoutError:
            logger.error("RequestedTaskManager.stop has timed out")

        self.task_computer.quit()

    def is_task_single_core(self, th: dt_tasks.TaskHeader) -> bool:
        env = self.get_environment_by_id(th.environment)
        if env is not None and isinstance(env, OldEnv):
            return env.is_single_core()
        return False

    def get_environment_by_id(
            self,
            env_id: str
    ) -> Optional[Union[OldEnv, NewEnv]]:
        """ Looks for the requested env_id in the new, then the old env_manager.
            Returns None when the environment is not found. """
        keeper = self.task_keeper
        if keeper.new_env_manager.enabled(env_id):
            return keeper.new_env_manager.environment(env_id)
        return keeper.old_env_manager.get_environment_by_id(env_id)

    def request_task_by_id(self, task_id: str) -> None:
        """ Requests task possibly after successful resource handshake. """
        try:
            task_header = self.task_keeper.task_headers[task_id]
        except KeyError:
            logger.debug("Task missing in TaskKeeper. task_id=%s", task_id)
            return
        self._request_task(task_header)

    def _request_random_task(self) -> None:
        """ If there is no task currently computing and time elapsed from last
            request exceeds the configured request interval, choose a random
            task from the network to compute on our machine. """

        if time.time() - self._last_task_request_time \
                < self.config_desc.task_request_interval:
            return

        if (not self.task_computer.compute_tasks) \
                or (not self.task_computer.runnable):
            return

        if not self.task_computer.can_take_work():
            return

        compatible_tasks = self.task_computer.compatible_tasks(
            set(self.task_keeper.supported_tasks))

        task_header = self.task_keeper.get_task(
            exclude=self.requested_tasks, supported_tasks=compatible_tasks)
        if task_header is None:
            return

        self._last_task_request_time = time.time()
        self.task_computer.stats.increase_stat('tasks_requested')

        def _request_task_error(e):
            logger.error(
                "Failed to request task: task_id=%r, exception=%r",
                task_header.task_id,
                e
            )
        # Unyielded deferred, fire and forget requesting a new task
        deferred = self._request_task(task_header)
        deferred.addErrback(_request_task_error)  # pylint: disable=no-member

    @inlineCallbacks
    # pylint: disable=too-many-return-statements,too-many-branches
    def _request_task(self, theader: dt_tasks.TaskHeader) -> Deferred:
        try:
            supported = self.should_accept_requestor(theader.task_owner.key)
            if self.config_desc.min_price > theader.max_price:
                supported = supported.join(SupportStatus.err({
                    UnsupportReason.MAX_PRICE: theader.max_price}))

            if (
                    self.client.concent_service.enabled
                    and self.client.concent_service.required_as_provider
                    and not theader.concent_enabled
            ):
                supported = supported.join(
                    SupportStatus.err({
                        UnsupportReason.CONCENT_REQUIRED: True,
                    }),
                )

            # prepare env for performance, should always exist at this point
            env_id = theader.environment
            env = self.get_environment_by_id(env_id)
            if env is None:
                supported = supported.join(
                    SupportStatus.err(
                        {UnsupportReason.ENVIRONMENT_MISSING: env_id}
                    )
                )

            if not supported.is_ok():
                logger.debug(
                    "Support status. task_id=%s supported=%s",
                    theader.task_id,
                    supported,
                )
                if self.task_archiver:
                    self.task_archiver.add_support_status(
                        theader.task_id,
                        supported,
                    )
                return None

            num_subtasks = 1
            # Check performance
            if isinstance(env, OldEnv):
                benchmark_result = env.get_benchmark_result()
                benchmark_score = benchmark_result.performance
                benchmark_cpu_usage = benchmark_result.cpu_usage
                if env.is_single_core():
                    num_subtasks = self.task_computer.free_cores
                    if num_subtasks == 0:
                        return None
            else:  # NewEnv
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.app_benchmark_manager.get(
                            theader.environment,
                            theader.environment_prerequisites),
                        loop=asyncio_main_loop())
                    app_benchmark = yield Deferred.fromFuture(future)
                except ComputationInProgress as error:
                    logger.debug(
                        "Not requesting task_id=%s: %r",
                        theader.task_id,
                        error)
                    return None
                except Exception:  # pylint: disable=broad-except
                    logger.exception("Cannot retrieve benchmark score")
                    return None
                benchmark_score = app_benchmark.score
                benchmark_cpu_usage = app_benchmark.cpu_usage

            # Check handshake
            handshake = self.resource_handshakes.get(theader.task_owner.key)
            if not handshake:
                logger.debug(
                    "Starting handshake. key_id=%r, task_id=%r",
                    theader.task_owner.key,
                    theader.task_id,
                )
                self.start_handshake(
                    key_id=theader.task_owner.key,
                    task_id=theader.task_id,
                )
                return None
            handshake.task_id = theader.task_id
            if not handshake.success():
                logger.debug(
                    "Handshake still in progress. key_id=%r, task_id=%r",
                    theader.task_owner.key,
                    theader.task_id,
                )
                return None

            market_strategy = self.task_manager\
                .get_provider_market_strategy_for_env(theader.environment)

            price = market_strategy.calculate_price(
                ProviderPricing(
                    price_per_wallclock_h=self.config_desc.min_price,
                    price_per_cpu_h=self.config_desc.price_per_cpu_h,
                ), theader.max_price, theader.task_owner.key)
            self.task_manager.add_comp_task_request(
                theader=theader, price=price,
                performance=benchmark_score
            )
            wtct = message.tasks.WantToComputeTask(
                perf_index=benchmark_score,
                cpu_usage=benchmark_cpu_usage,
                price=price,
                max_resource_size=self.config_desc.max_resource_size,
                max_memory_size=self.config_desc.max_memory_size,
                num_subtasks=num_subtasks,
                concent_enabled=self.client.concent_service.enabled
                if theader.concent_enabled else False,

                provider_public_key=self.keys_auth.key_id,
                provider_ethereum_address=self.keys_auth.eth_addr,
                task_header=theader,
            )
            msg_queue.put(
                node_id=theader.task_owner.key,
                msg=wtct,
            )
            timer.ProviderTTCDelayTimers.start(wtct.task_id)
            self.requested_tasks.add(theader.task_id)
            return theader.task_id
        except Exception as err:  # pylint: disable=broad-except
            logger.warning("Cannot send request for task: %s", err)
            logger.warning("Detailed traceback", exc_info=True)
            self.remove_task_header(theader.task_id)

        return None

    def task_given(
            self,
            msg: message.tasks.TaskToCompute,
    ) -> bool:
        if not self.task_computer.can_take_work():
            logger.error("Trying to assign a task, when it's already assigned")
            return False

        task_header: dt_tasks.TaskHeader = msg.want_to_compute_task.task_header

        cpu_time_limit = None
        task_class = self.task_manager.apps_manager.get_task_class_for_env(
            task_header.environment)
        if task_class.PROVIDER_MARKET_STRATEGY.SET_CPU_TIME_LIMIT:
            cpu_time_limit = task_helpers.calculate_max_usage(
                task_header.subtask_budget, msg.want_to_compute_task.price)

        self.task_computer.task_given(msg.compute_task_def, cpu_time_limit)

        resource_downloaded = functools.partial(
            self._resource_downloaded,
            msg.subtask_id,
            msg.requestor_id,
            msg.price)

        if task_header.environment_prerequisites:
            subtask_inputs_dir = self.task_computer.get_subtask_inputs_dir()
            resources_options = msg.resources_options or dict(options={})
            client_options = self.resource_manager.build_client_options(
                **resources_options.get('options', {}))

            deferred_list = [
                self.new_resource_manager.download(
                    resource_id,
                    subtask_inputs_dir,
                    client_options,
                ) for resource_id in msg.compute_task_def['resources']
            ]

            defer.gatherResults(
                deferred_list,
                consumeErrors=True,
            ).addCallback(
                lambda _: resource_downloaded()
            ).addCallbacks(
                lambda _: self.resource_collected(msg.task_id, msg.subtask_id),
                lambda e: self.resource_failure(msg.task_id, e))
        else:
            self.request_resource(
                msg.task_id,
                msg.subtask_id,
                msg.compute_task_def['resources'],
                msg.resources_options,
            )
            resource_downloaded()

        return True

    def _resource_downloaded(
            self,
            subtask_id: str,
            requestor_id: str,
            price: int,
    ) -> None:
        self.requested_tasks.clear()
        update_requestor_assigned_sum(requestor_id, price)
        dispatcher.send(
            signal='golem.subtask',
            event='started',
            subtask_id=subtask_id,
            price=price,
        )

    def resource_collected(
            self,
            task_id: str,
            subtask_id: Optional[str] = None
    ) -> bool:
        return self.task_computer.start_computation(task_id, subtask_id)

    def resource_failure(self, task_id: str, reason: str) -> None:
        if task_id not in self.task_computer.assigned_task_ids:
            logger.error("Resource failure for a wrong task, %s", task_id)
            return

        subtask_id = self.task_computer.assigned_subtask_id
        self.task_computer.task_interrupted(task_id)
        if subtask_id is not None:
            self.send_task_failed(
                subtask_id, task_id, f'Error downloading resources: {reason}')
        else:
            logger.error("Missing subtask info for task failure %s", task_id)

    def send_results(
            self,
            subtask_id: str,
            task_id: str,
            result: Optional[List[Path]] = None,
            task_api_result: Optional[Path] = None,
            stats: Optional[Dict] = None,
    ) -> None:
        if not result and not task_api_result:
            raise ValueError('No results to send')

        if subtask_id in self.results_to_send:
            raise RuntimeError("Incorrect subtask_id: {}".format(subtask_id))

        # this is purely for tests
        if self.config_desc.overwrite_results and result is not None:
            for file_path in result:
                shutil.copyfile(
                    src=self.config_desc.overwrite_results,
                    dst=file_path)

        header = self.task_keeper.task_headers[task_id]

        delay_time = 0.0
        last_sending_trial = 0
        stats = stats or {}
        if result is None:
            task_result: Tuple = (str(task_api_result),)
        else:
            task_result = tuple(result)

        wtr = WaitingTaskResult(
            task_id=task_id,
            subtask_id=subtask_id,
            result=task_result,
            last_sending_trial=last_sending_trial,
            delay_time=delay_time,
            owner=header.task_owner,
            stats=stats)

        def enqueue_computed_task_result():
            self.results_to_send[wtr.subtask_id] = wtr
            Trust.REQUESTED.increase(wtr.owner.key)

        if result:
            self._create_and_set_result_package(wtr)
            enqueue_computed_task_result()
            return

        def on_result_share_success(resource_id):
            wtr.package_sha1 = resource_id
            wtr.result_path = wtr.result[0]
            wtr.result_hash = resource_id
            enqueue_computed_task_result()

        def on_result_share_error(err):
            logger.error(
                "Cannot share resources for subtask_id=%s: %r",
                subtask_id, err)

        client_options = self.get_share_options(
            timeout=deadline_to_timeout(header.deadline))
        deferred = self.new_resource_manager.share(
            task_api_result,
            client_options)
        deferred.addCallbacks(  # pylint: disable=no-member
            on_result_share_success, on_result_share_error)

    def _create_and_set_result_package(self, wtr):
        task_result_manager = self.task_manager.task_result_manager
        client_options = self.get_share_options(
            timeout=self.RESULT_SHARE_TIMEOUT)

        wtr.result_secret = task_result_manager.gen_secret()
        result = task_result_manager.create(
            wtr,
            client_options,
            wtr.result_secret)

        (
            wtr.result_hash,
            wtr.result_path,
            wtr.package_sha1,
            wtr.result_size,
            wtr.package_path,
        ) = result

    def send_task_failed(
            self,
            subtask_id: str,
            task_id: str,
            err_msg: str,
            reason=message.TaskFailure.DEFAULT_REASON
    ) -> None:
        header = self.task_keeper.task_headers[task_id]

        if subtask_id not in self.failures_to_send:
            Trust.REQUESTED.decrease(header.task_owner.key)

            self.failures_to_send[subtask_id] = WaitingTaskFailure(
                task_id=task_id,
                subtask_id=subtask_id,
                err_msg=err_msg,
                owner=header.task_owner,
                reason=reason)

    def new_connection(self, session):
        if not self.active:
            session.disconnect(message.base.Disconnect.REASON.NoMoreMessages)
            return
        logger.debug(
            'Incoming TaskSession. address=%s:%d',
            session.address,
            session.port,
        )
        self.task_sessions_incoming.add(session)

    def disconnect(self):
        for node_id in list(self.sessions):
            try:
                task_session = self.sessions[node_id]
                if task_session is None:
                    # Pending connection
                    continue
                task_session.dropped()
                del self.sessions[node_id]
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Error closing session: %s", exc)

    def get_own_tasks_headers(self):
        old_headers = self.task_manager.get_tasks_headers()
        new_headers = self._get_and_sign_headers()
        return old_headers + new_headers

    def _get_and_sign_headers(self):
        started_tasks = self.requested_task_manager.get_started_tasks()
        signed_headers = []
        for db_task in started_tasks:
            # FIXME: store the value in RequestedTask
            # https://github.com/golemfactory/golem/pull/
            # 4926#discussion_r349627722
            subtask_budget = calculate_subtask_payment(
                db_task.max_price_per_hour,
                db_task.subtask_timeout
            )
            task_header = dt_tasks.TaskHeader(
                min_version=str(gconst.GOLEM_MIN_VERSION),
                task_id=db_task.task_id,
                environment=db_task.env_id,
                environment_prerequisites=db_task.prerequisites,
                task_owner=self.node,
                deadline=int(db_task.deadline.timestamp()),
                subtask_timeout=db_task.subtask_timeout,
                subtask_budget=subtask_budget,
                subtasks_count=db_task.max_subtasks,
                estimated_memory=db_task.min_memory,
                max_price=db_task.max_price_per_hour,
                concent_enabled=db_task.concent_enabled,
                timestamp=int(db_task.start_time.timestamp()),
            )
            task_header.sign(private_key=self.keys_auth._private_key)
            signed_headers.append(task_header)

        return signed_headers

    def get_others_tasks_headers(self) -> List[dt_tasks.TaskHeader]:
        return self.task_keeper.get_all_tasks()

    @inlineCallbacks
    def add_task_header(self, task_header: dt_tasks.TaskHeader):
        if not self._verify_header_sig(task_header):
            logger.info(
                'Invalid signature. task_id=%r, signature=%r',
                task_header.task_id,
                task_header.signature,
            )
            logger.debug("task_header=%r", task_header)
            return False
        if task_header.deadline < time.time():
            logger.info(
                "Task's deadline already in the past. task_id=%r",
                task_header.task_id
            )
            logger.debug("task_header=%r", task_header)
            return False

        if task_header.environment_prerequisites:
            image_name = task_header.environment_prerequisites['image']
            self._docker_image_discovered(image_name)

        try:
            if self.task_manager.is_my_task(task_header.task_id) or \
                    task_header.task_owner.key == self.node.key:
                return True  # Own tasks are not added to task keeper

            task_added = yield self.task_keeper.add_task_header(task_header)
            return task_added
        except Exception:  # pylint: disable=broad-except
            logger.exception("Task header validation failed")
        return False

    @classmethod
    def _verify_header_sig(cls, header: dt_tasks.TaskHeader):
        try:
            header.verify(public_key=decode_hex(header.task_owner.key))
        except msg_exceptions.CryptoError:
            logger.debug(
                'hdr verification failed. hdr.task_owner.key: %r',
                header.task_owner.key,
                exc_info=True,
            )
            return False
        return True

    @rpc_utils.expose('comp.tasks.known.delete')
    def remove_task_header(self, task_id) -> bool:
        self.requested_tasks.discard(task_id)
        return self.task_keeper.remove_task_header(task_id)

    def set_last_message(self, type_, t, msg, ip_addr, port):
        if len(self.last_messages) >= 5:
            self.last_messages = self.last_messages[-4:]

        self.last_messages.append([type_, t, ip_addr, port, msg])

    def _task_result_sent(self, subtask_id):
        return self.results_to_send.pop(subtask_id, None)

    @inlineCallbacks
    def change_config(
            self,
            config_desc: ClientConfigDescriptor,
            run_benchmarks: bool = False
    ) -> Deferred:  # pylint: disable=arguments-differ

        PendingConnectionsServer.change_config(self, config_desc)
        yield self.task_keeper.change_config(config_desc)
        yield self._change_task_computer_config(config_desc, run_benchmarks)

    @inlineCallbacks
    def _change_task_computer_config(
            self,
            config_desc: ClientConfigDescriptor,
            run_benchmarks: bool,
    ) -> Deferred:
        config_changed = yield self.task_computer.change_config(config_desc)
        if config_changed:
            self._remove_env_performance_scores()
            self.app_benchmark_manager.remove_benchmark_scores()
        elif not run_benchmarks:
            return

        self.task_computer.lock_config(True)
        deferred = Deferred()
        self.benchmark_manager.run_all_benchmarks(
            deferred.callback, deferred.errback)
        yield deferred
        self.task_computer.lock_config(False)

    def _remove_env_performance_scores(self) -> None:
        env_manager = self.task_keeper.new_env_manager
        for env_id in env_manager.environments():
            env_manager.remove_cached_performance(env_id)

    def get_task_computer_root(self):
        return os.path.join(self.client.datadir, "ComputerRes")

    def get_app_dir(self) -> Path:
        """ Get path to the directory where definitions for Task API apps are
            stored. """
        return Path(self.client.datadir) / "apps"

    def subtask_rejected(self, sender_node_id, subtask_id):
        """My (providers) results were rejected"""
        logger.debug("Subtask %r result rejected", subtask_id)
        self._task_result_sent(subtask_id)

        self._decrease_trust_payment(sender_node_id)
        # self.remove_task_header(task_id)
        # TODO Inform transaction system and task manager about rejected
        # subtask. Issue #2405

    # pylint:disable=too-many-arguments
    def subtask_accepted(
            self,
            sender_node_id: str,
            task_id: str,
            subtask_id: str,
            payer_address: str,
            value: int,
            accepted_ts: int):
        """My (providers) results were accepted"""
        logger.debug("Subtask %r result accepted", subtask_id)
        self._task_result_sent(subtask_id)
        self.client.transaction_system.expect_income(
            sender_node=sender_node_id,
            task_id=task_id,
            subtask_id=subtask_id,
            payer_address=payer_address,
            value=value,
            accepted_ts=accepted_ts,
        )

    def subtask_settled(self, sender_node_id, subtask_id, settled_ts):
        """My (provider's) results were accepted by the Concent"""
        logger.debug("Subtask %r settled by the Concent", subtask_id)
        self._task_result_sent(subtask_id)
        self.client.transaction_system.settle_income(
            sender_node_id, subtask_id, settled_ts)

    def subtask_waiting(self, task_id, subtask_id=None):
        logger.debug(
            "Requestor waits for subtask results."
            " task_id=%(task_id)s subtask_id=%(subtask_id)s",
            {
                'task_id': task_id,
                'subtask_id': subtask_id,
            },
        )
        # We can still try to request a subtask for this task next time.
        self.requested_tasks.discard(task_id)

    def subtask_failure(self, subtask_id, err):
        logger.info("Computation for task %r failed: %r.", subtask_id, err)
        node_id = self.task_manager.get_node_id_for_subtask(subtask_id)
        Trust.COMPUTED.decrease(node_id)
        self.task_manager.task_computation_failure(subtask_id, err)

    def accept_result(self, task_id, subtask_id, key_id, eth_address: str,
                      value: int, *, unlock_funds=True) -> TaskPayment:
        # FIXME: trust
        mod = min(
            max(self.task_manager.get_trust_mod(subtask_id), self.min_trust),
            self.max_trust)
        Trust.COMPUTED.increase(key_id, mod)
        payment = self.client.transaction_system.add_payment_info(
            node_id=key_id,
            task_id=task_id,
            subtask_id=subtask_id,
            value=value,
            eth_address=eth_address,
        )
        if unlock_funds:
            self.client.funds_locker.remove_subtask(task_id)
        logger.debug('Result accepted for subtask: %s Created payment ts: %r',
                     subtask_id, payment)
        return payment

    def income_listener(self, event='default', node_id=None, **kwargs):
        if event == 'confirmed':
            self._increase_trust_payment(node_id, kwargs['amount'])
        elif event == 'overdue_single':
            self._decrease_trust_payment(node_id)

    def finished_subtask_listener(self,  # pylint: disable=too-many-arguments
                                  event='default', subtask_id=None,
                                  min_performance=None, **_kwargs):

        if event != 'subtask_finished':
            return

        keeper = self.task_manager.comp_task_keeper

        try:
            task_id = keeper.get_task_id_for_subtask(subtask_id)
            header = keeper.get_task_header(task_id)
            performance = keeper.active_tasks[task_id].performance
            computation_time = timer.ProviderTimer.time

            update_requestor_efficiency(
                node_id=keeper.get_node_for_task_id(task_id),
                timeout=header.subtask_timeout,
                computation_time=computation_time,
                performance=performance,
                min_performance=min_performance,
            )

        except (KeyError, ValueError, AttributeError) as exc:
            logger.error("Finished subtask listener: %r", exc)
            return

    def finished_task_listener(self, event='default', task_id=None, op=None,
                               **_kwargs):
        if not (event == 'task_status_updated'
                and self.client.p2pservice):
            return
        if not (op in [TaskOp.FINISHED, TaskOp.TIMEOUT, TaskOp.ABORTED]):
            return
        self.client.p2pservice.remove_task(task_id)
        self.client.funds_locker.remove_task(task_id)
        if not self.requested_task_manager.has_unfinished_tasks():
            self.client.update_setting('accept_tasks', True)

    def _increase_trust_payment(self, node_id: str, amount: int):
        Trust.PAYMENT.increase(node_id, self.max_trust)
        update_requestor_paid_sum(node_id, amount)

    def _decrease_trust_payment(self, node_id: str):
        Trust.PAYMENT.decrease(node_id, self.max_trust)

    def reject_result(self, subtask_id, key_id):
        mod = min(
            max(self.task_manager.get_trust_mod(subtask_id), self.min_trust),
            self.max_trust)
        Trust.WRONG_COMPUTED.decrease(key_id, mod)

    def get_socket_addresses(self, node_info, prv_port=None, pub_port=None):
        """ Change node info into tcp addresses. Adds a suggested address.
        :param Node node_info: node information
        :param prv_port: private port that should be used
        :param pub_port: public port that should be used
        :return:
        """
        prv_port = prv_port or node_info.prv_port
        pub_port = pub_port or node_info.pub_port

        socket_addresses = super().get_socket_addresses(
            node_info=node_info,
            prv_port=prv_port,
            pub_port=pub_port
        )

        address = self.client.get_suggested_addr(node_info.key)
        if not address:
            return socket_addresses

        if self._is_address_valid(address, prv_port):
            socket_address = SocketAddress(address, prv_port)
            self._prepend_address(socket_addresses, socket_address)

        if self._is_address_valid(address, pub_port):
            socket_address = SocketAddress(address, pub_port)
            self._prepend_address(socket_addresses, socket_address)

        return socket_addresses[:MAX_CONNECT_SOCKET_ADDRESSES]

    def add_forwarded_session_request(self, key_id, conn_id):
        self.forwarded_session_requests[key_id] = dict(
            conn_id=conn_id, time=time.time())

    def get_min_performance_for_env(self, env_id: str) -> float:
        env = self.get_environment_by_id(env_id)
        if isinstance(env, OldEnv):
            return env.get_min_accepted_performance()
        # NewEnv
        # TODO: Implement minimum performance in new env
        return 0.0

    class RejectedReason(Enum):
        not_my_task = 'not my task'
        performance = 'performance'
        disk_size = 'disk size'
        memory_size = 'memory size'
        acl = 'acl'
        trust = 'trust'
        netmask = 'netmask'
        not_accepted = 'not accepted'

    def should_accept_provider(  # pylint: disable=too-many-return-statements
            self, node_id: str, ip_addr: str, task_id: str,
            provider_perf: float, max_memory_size: int,
            offer_hash: str) -> bool:

        node_name_id = short_node_id(node_id)
        ids = f'provider={node_name_id}, task_id={task_id}'

        if task_id in self.task_manager.tasks:
            task = self.task_manager.tasks[task_id]
            env_id = task.header.environment
            min_memory = task.header.estimated_memory
            mask = task.header.mask
            accept_client_verdict = task.should_accept_client(
                node_id,
                offer_hash)
        elif self.requested_task_manager.task_exists(task_id):
            req_task = self.requested_task_manager.get_requested_task(task_id)
            assert req_task, "Task missing due a race condition"
            env_id = req_task.env_id
            min_memory = req_task.min_memory
            mask = Mask(req_task.mask)
            # For compatibility purposes; the app decides to whom assign a task
            accept_client_verdict = AcceptClientVerdict.ACCEPTED
        else:
            logger.info('Cannot find task in my tasks: %s', ids)
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.not_my_task)
            return False

        min_accepted_perf = self.get_min_performance_for_env(env_id)

        if min_accepted_perf > int(provider_perf):
            logger.info(f'insufficient provider performance: {provider_perf}'
                        f' < {min_accepted_perf}; {ids}')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.performance,
                details={
                    'provider_perf': provider_perf,
                    'min_accepted_perf': min_accepted_perf,
                })
            return False

        if min_memory > (int(max_memory_size) * 1024):
            logger.info('insufficient provider memory size: '
                        f'{min_memory} B < {max_memory_size} KiB; {ids}')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.memory_size,
                details={
                    'memory_size': min_memory,
                    'max_memory_size': max_memory_size * 1024,
                })
            return False

        allowed, reason = self.acl.is_allowed(node_id)
        if allowed:
            allowed, reason = self.acl_ip.is_allowed(ip_addr)
        if not allowed:
            reason_msg = 'unknown reason' if reason is None else reason.value
            logger.info(f'provider is {reason_msg}; {ids}')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.acl,
                details={'acl_reason': reason_msg})
            return False

        trust = self.client.get_computing_trust(node_id)
        if trust < self.config_desc.computing_trust:
            logger.info(f'insufficient provider trust level: {trust} < '
                        f'{self.config_desc.computing_trust}; {ids}')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.trust,
                details={
                    'trust': trust,
                    'required_trust': self.config_desc.computing_trust,
                })
            return False

        if not mask.matches(decode_hex(node_id)):
            logger.info(f'network mask mismatch: {ids}')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.netmask)
            return False

        if accept_client_verdict != AcceptClientVerdict.ACCEPTED:
            logger.info(f'provider {node_id} is not allowed'
                        f' for this task at this moment '
                        f'(either waiting for results or previously failed)')
            self.notify_provider_rejected(
                node_id=node_id, task_id=task_id,
                reason=self.RejectedReason.not_accepted,
                details={
                    'verdict': accept_client_verdict.value,
                })
            return False

        logger.debug('provider can be accepted %s', ids)
        return True

    @classmethod
    def notify_provider_rejected(cls, node_id: str, task_id: str,
                                 reason: RejectedReason,
                                 details: Optional[Dict[str, Any]] = None):
        dispatcher.send(
            signal='golem.taskserver',
            event='provider_rejected',
            node_id=node_id,
            task_id=task_id,
            reason=reason.value,
            details=details,
        )

    def should_accept_requestor(self, node_id):
        allowed, reason = self.acl.is_allowed(node_id)
        if not allowed:
            short_id = short_node_id(node_id)
            logger.info('requestor is %s. node=%s', reason, short_id)
            return SupportStatus.err({UnsupportReason.DENY_LIST: node_id})
        trust = self.client.get_requesting_trust(node_id)
        logger.debug("Requesting trust level: %r", trust)
        if trust >= self.config_desc.requesting_trust:
            return SupportStatus.ok()
        return SupportStatus.err({UnsupportReason.REQUESTOR_TRUST: trust})

    @rpc_utils.expose('net.peer.disallow')
    def disallow_node(
            self,
            node_id: Union[str, list],
            timeout_seconds: int = -1,
            persist: bool = False
    ) -> None:
        if isinstance(node_id, str):
            node_id = [node_id]
        for item in node_id:
            self.acl.disallow(item, timeout_seconds, persist)

    @rpc_utils.expose('net.peer.block_ip')
    def disallow_ip(self, ip: Union[str, list],
                    timeout_seconds: int = -1) -> None:
        if isinstance(ip, str):
            ip = [ip]
        for item in ip:
            self.acl_ip.disallow(item, timeout_seconds)

    @rpc_utils.expose('net.peer.allow')
    def allow_node(self, node_id: Union[str, list],
                   persist: bool = True) -> None:
        if isinstance(node_id, str):
            node_id = [node_id]
        for item in node_id:
            self.acl.allow(item, persist)

    @rpc_utils.expose('net.peer.allow_ip')
    def allow_ip(self, ip: Union[str, list], persist: bool = True) -> None:
        if isinstance(ip, str):
            ip = [ip]
        for item in ip:
            self.acl_ip.allow(item, persist)

    @rpc_utils.expose('net.peer.acl')
    def acl_status(self) -> Dict:
        return self.acl.status().to_message()

    @rpc_utils.expose('net.peer.acl_ip')
    def acl_ip_status(self) -> Dict:
        return self.acl_ip.status().to_message()

    @rpc_utils.expose('net.peer.acl.new')
    def acl_setup(self, default_rule: str, exceptions: List[str]) -> None:
        new_acl = setup_acl(self.client,
                            AclRule[default_rule],
                            exceptions)
        self.acl = new_acl

    def _sync_forwarded_session_requests(self):
        now = time.time()
        for key_id, data in list(self.forwarded_session_requests.items()):
            if not data:
                del self.forwarded_session_requests[key_id]
                continue
            if now - data['time'] >= self.forwarded_session_request_timeout:
                logger.debug('connection timeout: %s', data)
                del self.forwarded_session_requests[key_id]
                self.final_conn_failure(data['conn_id'])

    def _listening_established(self, port: int) -> None:
        logger.debug('_listening_established(%r)', port)
        self.cur_port = port
        logger.info(" Port {} opened - listening".format(self.cur_port))
        self.node.prv_port = self.cur_port
        self.task_manager.node = self.node

    def _listening_failure(self, **kwargs):
        logger.error("Listening on ports {} to {} failure".format(
            self.config_desc.start_port, self.config_desc.end_port))
        # FIXME: some graceful terminations should take place here. #1287
        # sys.exit(0)

    #############################
    # SYNC METHODS
    #############################
    def __remove_old_tasks(self):
        self.task_keeper.remove_old_tasks()
        self.task_manager.comp_task_keeper.remove_old_tasks()
        nodes_with_timeouts = self.task_manager.check_timeouts()
        for node_id in nodes_with_timeouts:
            Trust.COMPUTED.decrease(node_id)

    def _send_waiting_results(self):
        for subtask_id in list(self.results_to_send.keys()):
            wtr: WaitingTaskResult = self.results_to_send[subtask_id]
            now = time.time()

            if not wtr.already_sending:
                if now - wtr.last_sending_trial > wtr.delay_time:
                    wtr.already_sending = True
                    wtr.last_sending_trial = now
                    helpers.send_report_computed_task(
                        task_server=self,
                        waiting_task_result=wtr,
                    )

        for wtf in list(self.failures_to_send.values()):
            helpers.send_task_failure(
                waiting_task_failure=wtf,
            )
        self.failures_to_send.clear()

    # CONFIGURATION METHODS
    #############################
    @staticmethod
    def __get_task_manager_root(datadir):
        return os.path.join(datadir, "ComputerRes")


@dataclass
class WaitingTaskResult:
    delay_time: float
    last_sending_trial: int
    owner: 'dt_p2p.Node'
    result: Tuple
    subtask_id: str
    task_id: str

    already_sending: bool = False
    package_path: Optional[str] = None
    package_sha1: Optional[str] = None
    result_hash: Optional[str] = None
    result_path: Optional[str] = None
    result_secret: Optional[str] = None
    result_sha1: Optional[str] = None
    result_size: int = 0
    stats: Dict = field(default_factory=dict)


@dataclass
class WaitingTaskFailure:
    err_msg: str
    owner: 'dt_p2p.Node'
    subtask_id: str
    task_id: str
    reason: message.TaskFailure.REASON
