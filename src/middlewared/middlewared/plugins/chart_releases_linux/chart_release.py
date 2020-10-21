import asyncio
import copy
import errno
import os
import shutil
import tempfile
import yaml

from middlewared.schema import accepts, Dict, Str
from middlewared.service import CallError, CRUDService, filterable, job, private

from .utils import run, get_namespace, get_storage_class_name


class ChartReleaseService(CRUDService):

    class Config:
        namespace = 'chart.release'

    @filterable
    async def query(self, filters=None, options=None):
        return await self.middleware.call('chart.release.query_releases', filters, options)

    @private
    async def normalise_and_validate_values(self, item_details, values, update, release_ds):
        dict_obj = await self.middleware.call('chart.release.validate_values', item_details, values, update)
        return await self.middleware.call(
            'chart.release.get_normalised_values', dict_obj, values, update, {
                'release': {
                    'name': release_ds.split('/')[-1],
                    'dataset': release_ds,
                    'path': os.path.join('/mnt', release_ds),
                },
                'actions': [],
            }
        )

    @private
    async def perform_actions(self, context):
        for action in context['actions']:
            await self.middleware.call(f'chart.release.{action["method"]}', *action['args'])

    @accepts(
        Dict(
            'chart_release_create',
            Dict('values', additional_attrs=True),
            Str('catalog', required=True),
            Str('item', required=True),
            Str('release_name', required=True),
            Str('train', default='charts'),
            Str('version', required=True),
        )
    )
    @job(lock=lambda args: f'chart_release_create_{args[0]["release_name"]}')
    async def do_create(self, job, data):
        """
        Create a chart release for a catalog item.

        `release_name` is the name which will be used to identify the created chart release.

        `catalog` is a valid catalog id where system will look for catalog `item` details.

        `train` is which train to look for under `catalog` i.e stable / testing etc.

        `version` specifies the catalog `item` version.

        `values` is configuration specified for the catalog item version in question which will be used to
        create the chart release.
        """
        await self.middleware.call('kubernetes.validate_k8s_setup')
        if await self.middleware.call('chart.release.query', [['id', '=', data['release_name']]]):
            raise CallError(f'Chart release with {data["release_name"]} already exists.', errno=errno.EEXIST)

        catalog = await self.middleware.call(
            'catalog.query', [['id', '=', data['catalog']]], {'get': True, 'extra': {'item_details': True}}
        )
        if data['train'] not in catalog['trains']:
            raise CallError(f'Unable to locate "{data["train"]}" catalog train.', errno=errno.ENOENT)
        if data['item'] not in catalog['trains'][data['train']]:
            raise CallError(f'Unable to locate "{data["item"]}" catalog item.', errno=errno.ENOENT)
        if data['version'] not in catalog['trains'][data['train']][data['item']]['versions']:
            raise CallError(f'Unable to locate "{data["version"]}" catalog item version.', errno=errno.ENOENT)

        item_details = catalog['trains'][data['train']][data['item']]['versions'][data['version']]
        await self.middleware.call('catalog.version_supported_error_check', item_details)

        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', data['release_name'])
        # The idea is to validate the values provided first and if it passes our validation test, we
        # can move forward with setting up the datasets and installing the catalog item
        default_values = item_details['values']
        new_values = copy.deepcopy(default_values)
        new_values.update(data['values'])
        new_values, context = await self.normalise_and_validate_values(item_details, new_values, False, release_ds)

        job.set_progress(25, 'Initial Validation completed')

        # Now that we have completed validation for the item in question wrt values provided,
        # we will now perform the following steps
        # 1) Create release datasets
        # 2) Copy chart version into release/charts dataset
        # 3) Install the helm chart
        # 4) Create storage class
        storage_class_name = await get_storage_class_name(data['release_name'])
        try:
            for dataset in await self.release_datasets(release_ds):
                if not await self.middleware.call('pool.dataset.query', [['id', '=', dataset]]):
                    await self.middleware.call('pool.dataset.create', {'name': dataset, 'type': 'FILESYSTEM'})

            chart_path = os.path.join('/mnt', release_ds, 'charts', data['version'])
            await self.middleware.run_in_thread(lambda: shutil.copytree(item_details['location'], chart_path))

            # Before finally installing the release, we will perform any actions which might be required
            # for the release to function like creating/deleting ix-volumes
            await self.perform_actions(context)

            job.set_progress(75, 'Installing Catalog Item')

            with tempfile.NamedTemporaryFile(mode='w+') as f:
                f.write(yaml.dump(new_values))
                f.flush()
                # We will install the chart now and force the installation in an ix based namespace
                # https://github.com/helm/helm/issues/5465#issuecomment-473942223
                cp = await run(
                    [
                        'helm', 'install', data['release_name'], chart_path, '-n',
                        get_namespace(data['release_name']), '--create-namespace', '-f', f.name,
                    ],
                    check=False,
                )
            if cp.returncode:
                raise CallError(f'Failed to install catalog item: {cp.stderr}')

            storage_class = await self.middleware.call('k8s.storage_class.retrieve_storage_class_manifest')
            storage_class['metadata']['name'] = storage_class_name
            storage_class['parameters']['poolname'] = os.path.join(release_ds, 'volumes')
            if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
                # It should not exist already, but even if it does, that's not fatal
                await self.middleware.call('k8s.storage_class.update', storage_class_name, storage_class)
            else:
                await self.middleware.call('k8s.storage_class.create', storage_class)

            job.set_progress(95, 'Syncing chart release secrets')
            await self.middleware.call(
                'chart.release.sync_secrets_for_release',
                data['release_name'], data['catalog'], data['train'],
            )
        except Exception:
            # Do a rollback here
            # Let's uninstall the release as well if it did get installed ( it is possible this might have happened )
            if await self.middleware.call('chart.release.query', [['id', '=', data['release_name']]]):
                delete_job = await self.middleware.call('chart.release.delete', data['release_name'])
                await delete_job.wait()
                if delete_job.error:
                    self.logger.error('Failed to uninstall helm chart release: %s', delete_job.error)
            else:
                await self.remove_storage_class_and_dataset(data['release_name'])

            raise
        else:
            job.set_progress(100, 'Chart release created')
            return await self.get_instance(data['release_name'])

    @accepts(
        Str('chart_release'),
        Dict(
            'chart_release_update',
            Dict('values', additional_attrs=True),
        )
    )
    async def do_update(self, chart_release, data):
        """
        Update an existing chart release.

        `values` is configuration specified for the catalog item version in question which will be used to
        create the chart release.
        """
        release = await self.get_instance(chart_release)
        chart_path = os.path.join(release['path'], 'charts', release['chart_metadata']['version'])
        if not os.path.exists(chart_path):
            raise CallError(
                f'Unable to locate {chart_path!r} chart version for updating {chart_release!r} chart release',
                errno=errno.ENOENT
            )

        version_details = await self.middleware.call('catalog.item_version_details', chart_path)
        config = release['config']
        config.update(data['values'])
        config, context = await self.normalise_and_validate_values(version_details, config, True, release['dataset'])

        await self.perform_actions(context)

        with tempfile.NamedTemporaryFile(mode='w+') as f:
            f.write(yaml.dump(config))
            f.flush()

            cp = await run(
                ['helm', 'upgrade', chart_release, chart_path, '-n', get_namespace(chart_release), '-f', f.name],
                check=False,
            )
            if cp.returncode:
                raise CallError(f'Failed to update chart release: {cp.stderr.decode()}')

        await self.middleware.call(
            'chart.release.sync_secrets_for_release', chart_release, release['catalog'], release['catalog_train'],
        )

        return await self.get_instance(chart_release)

    @accepts(Str('release_name'))
    @job(lock=lambda args: f'chart_release_delete_{args[0]}')
    async def do_delete(self, job, release_name):
        """
        Delete existing chart release.

        This will delete the chart release from the kubernetes cluster and also remove any associated volumes / data.
        To clarify, host path volumes will not be deleted which live outside the chart release dataset.
        """
        # For delete we will uninstall the release first and then remove the associated datasets
        await self.middleware.call('kubernetes.validate_k8s_setup')
        await self.get_instance(release_name)

        cp = await run(['helm', 'uninstall', release_name, '-n', get_namespace(release_name)], check=False)
        if cp.returncode:
            raise CallError(f'Unable to uninstall "{release_name}" chart release: {cp.stderr}')

        job.set_progress(50, f'Uninstalled {release_name}')
        # wait for release to uninstall properly, helm right now does not support a flag for this but
        # a feature request is open in the community https://github.com/helm/helm/issues/2378
        while await self.middleware.call(
            'k8s.pod.query', [['metadata.namespace', '=', get_namespace(release_name)]],
        ):
            job.set_progress(75, f'Waiting for {release_name!r} pods to terminate')
            await asyncio.sleep(5)

        await self.remove_storage_class_and_dataset(release_name, job)

        job.set_progress(100, f'{release_name!r} chart release deleted')
        return True

    @private
    async def remove_storage_class_and_dataset(self, release_name, job=None):
        storage_class_name = await get_storage_class_name(release_name)
        if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
            if job:
                job.set_progress(85, f'Removing {release_name!r} storage class')
            try:
                await self.middleware.call('k8s.storage_class.delete', storage_class_name)
            except Exception as e:
                self.logger.error('Failed to remove %r storage class: %s', storage_class_name, e)

        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', release_name)
        if await self.middleware.call('pool.dataset.query', [['id', '=', release_ds]]):
            if job:
                job.set_progress(95, f'Removing {release_ds!r} dataset')
            await self.middleware.call('zfs.dataset.delete', release_ds, {'recursive': True, 'force': True})

    @private
    async def release_datasets(self, release_dataset):
        return [release_dataset] + [
            os.path.join(release_dataset, k) for k in ('charts', 'volumes', 'volumes/ix_volumes')
        ]
