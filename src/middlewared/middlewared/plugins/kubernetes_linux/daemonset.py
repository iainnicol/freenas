from kubernetes_asyncio import client

from middlewared.schema import Dict, Str
from middlewared.service import accepts, CallError, CRUDService, filterable
from middlewared.utils import filter_list

from .k8s import api_client


class KubernetesDaemonsetService(CRUDService):

    class Config:
        namespace = 'k8s.daemonset'
        private = True

    @filterable
    async def query(self, filters=None, options=None):
        async with api_client() as (api, context):
            return filter_list(
                [d.to_dict() for d in (await context['apps_api'].list_daemon_set_for_all_namespaces()).items],
                filters, options
            )

    @accepts(
        Dict(
            'daemonset_create',
            Str('namespace', required=True),
            Dict('body', additional_attrs=True, required=True),
        )
    )
    async def do_create(self, data):
        async with api_client() as (api, context):
            try:
                context['apps_api'].create_namespaced_daemon_set(namespace=data['namespace'], body=data['body'])
            except client.exceptions.ApiException as e:
                raise CallError(f'Unable to create daemonset: {e}')

    @accepts(
        Str('name'),
        Dict(
            'daemonset_delete_options',
            Str('namespace', required=True),
        )
    )
    async def do_delete(self, name, options):
        async with api_client() as (api, context):
            try:
                await context['apps_api'].delete_namespaced_daemon_set(name, options['namespace'])
            except client.exceptions.ApiException as e:
                raise CallError(f'Unable to delete daemonset: {e}')
