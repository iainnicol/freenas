import os

from middlewared.schema import accepts, Str
from middlewared.service import job, periodic, private, Service

from .utils import pull_clone_repository


class CatalogService(Service):

    @accepts()
    @periodic(interval=864000)
    @job(lock='sync_catalogs')
    async def sync_all(self, job):
        """
        Refresh all available catalogs from upstream.
        """
        catalogs = await self.middleware.call('catalog.query')
        catalog_len = len(catalogs)
        for index, catalog in enumerate(catalogs):
            job.set_progress(((index + 1) / catalog_len) * 100, f'Syncing {catalog["id"]} catalog')
            try:
                await self.middleware.call('catalog.sync', catalog['id'])
            except Exception as e:
                self.logger.error('Failed to sync %r catalog: %s', catalog['id'], e)

    @accepts(Str('label', required=True))
    async def sync(self, catalog_label):
        """
        Sync `label` catalog to retrieve latest changes from upstream.
        """
        await self.middleware.call('catalog.items', catalog_label, {'cache': False})

    @private
    def update_git_repository(self, catalog, raise_exception=False):
        return pull_clone_repository(
            catalog['repository'], os.path.dirname(catalog['location']), catalog['branch'],
            raise_exception=raise_exception,
        )
