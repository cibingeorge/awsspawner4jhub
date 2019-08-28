import logging
import os
import string
from concurrent.futures import ThreadPoolExecutor

import boto3
import escapism
from jupyterhub.spawner import Spawner
from tornado import gen
from traitlets import (
    Integer,
    Unicode,
    Dict
)
from traitlets.config import LoggingConfigurable

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class EcsTaskSpawner(Spawner):
    """
    ECS Task Spawner
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        """ Creates and boots a new server to host the worker instance."""
        self.log.info("function create_new_instance %s" % self.user.name)
        self.ecs_client = boto3.client('ecs')
        self.ec2_client = boto3.client('ec2')

    strategy = Unicode(
        "ECSxEC2SpawnerHandler",
        config=True,
        help="""
        Indicates if the ECS Spawner mechanism must create an EC2 instance itself, or let ECS to choose one for us.
        """
    )

    strategy_parms = Dict(
        {},
        config=True,
        help="""
        Strategy parameters.
        """
    )

    ip = Unicode(
        '0.0.0.0',
        config=True,
        help="""
        The IP address (or hostname) the single-user server should listen on.
        """
    )

    @property
    def executor(self):
        """single global executor"""
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(1)
        return cls._executor

    def _get_spawner_handler(self):
        """
        Return the right handler based on the strategy
        :return SpawnerHandler: a handler
        """
        if self.strategy == 'ECSxEC2SpawnerHandler':
            return ECSxEC2SpawnerHandler(self, **self.strategy_parms)
        if self.strategy == 'ECSSpawnerHandler':
            return ECSSpawnerHandler(self, **self.strategy_parms)
        if self.strategy == 'EC2SpawnerHandler':
            return EC2SpawnerHandler(self, **self.strategy_parms)

        raise ValueError("Strategy not properly specified")

    @gen.coroutine
    def start(self):
        self.log.info("function start for user %s" % self.user.name)
        handler = self._get_spawner_handler()

        result = yield self.executor.submit(handler.start)

        return result

    @gen.coroutine
    def stop(self, now=False):
        self.log.info("function stop called for %s" % self.user.name)

        return (yield  self._get_spawner_handler().stop())

        self.clear_state()

    @gen.coroutine
    def poll(self):
        self.log.debug("function poll for user %s" % self.user.name)

        return (yield self._get_spawner_handler().poll())


class SpawnerHandler(LoggingConfigurable):
    """
    Generic Handler
    """
    def __init__(self, spawner, **kwargs):
        self.spawner = spawner
        self.user = spawner.user
        self.hub = spawner.hub
        self.ecs_client = spawner.ecs_client
        self.ec2_client = spawner.ec2_client

    def get_env(self):
        return self.spawner.get_env()

    @gen.coroutine
    def start(self):
        pass

    @gen.coroutine
    def stop(self):
        pass

    @gen.coroutine
    def poll(self):
        pass


class ECSSpawnerHandler(SpawnerHandler):
    """
        Using ECS Task:
    """
    ecs_task_definition = Unicode(
        "",
        config=True,
        help="""
            Name of the Task Definition to be used when running the task.
        """
    )

    def __init__(self, spawner, cluster_name, ecs_task_definition, **kwargs):
        super().__init__(spawner)
        self.cluster_name = cluster_name
        self.ecs_task_definition = ecs_task_definition

    @gen.coroutine
    def start(self):
        task = yield self.get_task()
        if task is None:
            ip_address = yield self._create_new_task()
            return ip_address, self.port
        raise ValueError('Not handled yet')

    @gen.coroutine
    def stop(self):
        task = yield self.get_task()

        # Only Stop the task
        self.ecs_client.stop_task(
            cluster=self.cluster_name,
            task=task['taskArn']
        )

    @gen.coroutine
    def poll(self):
        pass

    @gen.coroutine
    def get_task(self):
        tasks = self.ecs_client.list_tasks(
            cluster=self.cluster_name,
            startedBy=self._get_task_identifier(),
            desiredStatus='RUNNING'
        )
        if tasks and len(tasks['taskArns']) > 0:
            return self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=[
                    tasks['taskArns'][0]
                ]

            )['tasks'][0]
        else:
            return None

    def _get_task_identifier(self):
        """
        Return Task identifier
        :return:
        """
        return 'EcsTaskSpawner:' + self.user.name

    @gen.coroutine
    def _create_new_task(self):
        self.log.info("function create new task for user %s" % self.user.name)
        task_def_arn = yield self._get_task_definition()

        env = self.get_env()
        env['JPY_USER'] = self.user.name
        env['JPY_BASE_URL'] = self.user.server.base_url
        env['JPY_COOKIE_NAME'] = self.user.server.cookie_name

        container_env = self._expand_env(env)

        self.log.info("starting ecs task for user %s" % self.user.name)

        task = self.ecs_client.run_task(taskDefinition=task_def_arn,
                                        cluster=self.cluster_name,
                                        startedBy=self._get_task_identifier(),
                                        overrides={
                                            'containerOverrides': [
                                                {
                                                    'name': 'hello-world',
                                                    'environment': container_env
                                                }
                                            ]
                                        })['tasks'][0]

        waiter = self.ecs_client.get_waiter('tasks_running')
        waiter.wait(cluster=self.cluster_name, tasks=[task['taskArn']])

        self.log.info("ecs task up and running for %s" % self.user.name)

        raise ValueError("Still todo, get ip of the container")

    @gen.coroutine
    def _get_task_definition(self):
        """
        Return the Arn of the Task Definition to be used when creating the task
        :return:
        """
        self.log.info("function get task definition for user %s" % self.user.name)

        if self.ecs_task_definition != '':
            task_def = self.ecs_client.describe_task_definition(taskDefinition=self.ecs_task_definition)['taskDefinition']
            return task_def['taskDefinitionArn']

        task_def = {
            'family': 'hello-world',
            'volumes': [],
            'containerDefinitions': [
                {
                    'memory': 1024,
                    'cpu': 0,
                    'essential': True,
                    'name': 'hello-world',
                    'image': 'jupyter/scipy-notebook:ae885c0a6226',
                    'portMappings': [
                        {
                            'containerPort': 8888,
                            'hostPort': 8888,
                            'protocol': 'tcp'
                        }
                    ],
                    'command': [
                        'start-notebook.sh',
                    ],
                }
            ]
        }

        response = self.ecs_client.register_task_definition(**task_def)
        task_def_arn = response['taskDefinition']['taskDefinitionArn']

        return task_def_arn

    def _expand_env(self, env):
        """
        Expand get_env to ECS task environment
        """
        result = []

        if env:
            for key in env.keys():
                entry = {
                    'name': key,
                    'value': env.get(key)
                }
                result.append(entry)

        return result

    def get_env(self):
        env = super().get_env()

        env['JPY_HUB_API_URL'] = 'http://' + os.environ.get('HUB_HOST_IP', '127.0.0.1') + ':8081/jupyter/hub/api'
        # env['JPY_HUB_API_URL'] = self.hub.api_url
        env['JPY_HUB_PREFIX'] = self.hub.server.base_url

        env.update(dict(
            JPY_USER=self.user.name,
            JPY_COOKIE_NAME=self.user.server.cookie_name,
            JPY_BASE_URL=self.user.server.base_url,
            JPY_HUB_PREFIX=self.hub.server.base_url
        ))

        return env
