"""
byceps.cli.command.delete_site
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Delete a site and all related data.

:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import click
from flask.cli import with_appcontext
from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.news.dbmodels import DbNewsChannel
from byceps.services.page.dbmodels import (
    DbCurrentPageVersionAssociation,
    DbPage,
    DbPageVersion,
)
from byceps.services.site import site_service
from byceps.services.site.dbmodels import DbSite, DbSiteSetting, site_news_channels
from byceps.services.site.models import SiteID
from byceps.services.site_navigation.dbmodels import DbNavItem, DbNavMenu


@click.command()
@click.argument('site_id')
@click.option(
    '--force',
    is_flag=True,
    help='Skip confirmation prompt',
)
@click.option(
    '--nullify-news-channels',
    is_flag=True,
    help='Set announcement_site_id to NULL for news channels instead of blocking',
)
@with_appcontext
def delete_site(
    site_id: SiteID, force: bool, nullify_news_channels: bool
) -> None:
    """Delete a site and all related data."""
    site = site_service.find_site(site_id)

    if site is None:
        click.secho(f'Site "{site_id}" not found.', fg='red')
        return

    # Get statistics before deletion
    stats = _gather_statistics(site_id)

    if not _has_any_data(stats):
        click.secho(
            f'Site "{site_id}" has no related data.',
            fg='yellow',
        )
        _confirm_and_delete_site(site_id, site.title, force)
        return

    # Display what will be deleted
    click.echo(f'\nSite: {site.title} ({site_id})')
    click.echo('=' * 60)
    _display_statistics(stats)

    # Check for blockers
    blockers = _check_blockers(site_id, nullify_news_channels)
    if blockers:
        click.echo()
        click.secho('Cannot delete site due to the following issues:', fg='red')
        for blocker in blockers:
            click.echo(f'  - {blocker}')
        return

    # Confirm deletion unless --force is used
    if not force:
        click.echo()
        if nullify_news_channels and stats['news_channels_with_announcement'] > 0:
            click.echo(
                f'Note: {stats["news_channels_with_announcement"]} news channel(s) '
                'will have their announcement_site_id set to NULL.'
            )
        if not click.confirm(
            'Do you want to delete this site and all related data?'
        ):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    # Perform deletion
    click.echo('\nDeleting site and related data...')
    result = _delete_site_data(site_id, nullify_news_channels)

    if result['success']:
        click.echo()
        click.secho('Successfully deleted:', fg='green')
        for key, count in sorted(result['counts'].items()):
            if count > 0:
                click.echo(f'  - {count} {key}')
    else:
        click.echo()
        click.secho(f'Error: {result["error"]}', fg='red')


def _gather_statistics(site_id: SiteID) -> dict[str, int]:
    """Gather statistics about site-related data."""
    return {
        'pages': _count_pages_for_site(site_id),
        'page_versions': _count_page_versions_for_site(site_id),
        'nav_menus': _count_nav_menus_for_site(site_id),
        'nav_items': _count_nav_items_for_site(site_id),
        'site_settings': _count_site_settings_for_site(site_id),
        'news_channel_associations': _count_news_channel_associations_for_site(
            site_id
        ),
        'news_channels_with_announcement': _count_news_channels_with_announcement_site(
            site_id
        ),
    }


def _has_any_data(stats: dict[str, int]) -> bool:
    """Check if there is any related data."""
    return any(count > 0 for count in stats.values())


def _display_statistics(stats: dict[str, int]) -> None:
    """Display statistics about what will be deleted."""
    click.echo('Related data that will be deleted:')
    for key, count in sorted(stats.items()):
        if count > 0:
            click.echo(f'  - {key}: {count}')


def _check_blockers(
    site_id: SiteID, nullify_news_channels: bool
) -> list[str]:
    """Check for conditions that prevent deletion."""
    blockers = []

    # News channels with announcement_site_id pointing to this site
    if not nullify_news_channels:
        news_channel_count = _count_news_channels_with_announcement_site(site_id)
        if news_channel_count > 0:
            blockers.append(
                f'{news_channel_count} news channel(s) use this site for announcements. '
                'Use --nullify-news-channels to set their announcement_site_id to NULL.'
            )

    return blockers


def _count_pages_for_site(site_id: SiteID) -> int:
    """Count pages for this site."""
    return (
        db.session.scalar(
            select(db.func.count(DbPage.id)).filter_by(site_id=site_id)
        )
        or 0
    )


def _count_page_versions_for_site(site_id: SiteID) -> int:
    """Count page versions for pages of this site."""
    return (
        db.session.scalar(
            select(db.func.count(DbPageVersion.id))
            .join(DbPage)
            .filter(DbPage.site_id == site_id)
        )
        or 0
    )


def _count_nav_menus_for_site(site_id: SiteID) -> int:
    """Count navigation menus for this site."""
    return (
        db.session.scalar(
            select(db.func.count(DbNavMenu.id)).filter_by(site_id=site_id)
        )
        or 0
    )


def _count_nav_items_for_site(site_id: SiteID) -> int:
    """Count navigation items for this site's menus."""
    return (
        db.session.scalar(
            select(db.func.count(DbNavItem.id))
            .join(DbNavMenu)
            .filter(DbNavMenu.site_id == site_id)
        )
        or 0
    )


def _count_site_settings_for_site(site_id: SiteID) -> int:
    """Count site settings for this site."""
    return (
        db.session.scalar(
            select(db.func.count(DbSiteSetting.site_id)).filter_by(
                site_id=site_id
            )
        )
        or 0
    )


def _count_news_channel_associations_for_site(site_id: SiteID) -> int:
    """Count news channel associations for this site."""
    return (
        db.session.scalar(
            select(db.func.count())
            .select_from(site_news_channels)
            .where(site_news_channels.c.site_id == site_id)
        )
        or 0
    )


def _count_news_channels_with_announcement_site(site_id: SiteID) -> int:
    """Count news channels that use this site for announcements."""
    return (
        db.session.scalar(
            select(db.func.count(DbNewsChannel.id)).filter_by(
                announcement_site_id=site_id
            )
        )
        or 0
    )


def _confirm_and_delete_site(
    site_id: SiteID, site_title: str, force: bool
) -> None:
    """Delete site with no related data after confirmation."""
    if not force:
        click.echo()
        if not click.confirm(f'Do you want to delete site "{site_title}"?'):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    site_service.delete_site(site_id)
    click.echo()
    click.secho(f'Site "{site_title}" has been deleted.', fg='green')


def _delete_site_data(
    site_id: SiteID, nullify_news_channels: bool
) -> dict:
    """Delete all site-related data."""
    counts = {}

    try:
        # Nullify news channels' announcement_site_id if requested
        if nullify_news_channels:
            news_channels_nullified = db.session.execute(
                db.update(DbNewsChannel)
                .where(DbNewsChannel.announcement_site_id == site_id)
                .values(announcement_site_id=None)
            ).rowcount
            counts['news_channels_nullified'] = news_channels_nullified

        # Get nav menu IDs for this site (for deleting nav items)
        nav_menu_ids = db.session.scalars(
            select(DbNavMenu.id).filter_by(site_id=site_id)
        ).all()

        # Delete navigation items for this site's menus
        nav_items_count = 0
        if nav_menu_ids:
            nav_items_count = db.session.execute(
                delete(DbNavItem).filter(DbNavItem.menu_id.in_(nav_menu_ids))
            ).rowcount
        counts['nav_items'] = nav_items_count

        # Delete navigation menus (handle self-referential parent_menu_id)
        # First, set all parent_menu_id to NULL for menus of this site
        db.session.execute(
            db.update(DbNavMenu)
            .where(DbNavMenu.site_id == site_id)
            .values(parent_menu_id=None)
        )

        # Then delete the menus
        nav_menus_count = db.session.execute(
            delete(DbNavMenu).filter_by(site_id=site_id)
        ).rowcount
        counts['nav_menus'] = nav_menus_count

        # Get page IDs for this site (for deleting page versions)
        page_ids = db.session.scalars(
            select(DbPage.id).filter_by(site_id=site_id)
        ).all()

        # Delete current page version associations
        current_version_assoc_count = 0
        if page_ids:
            current_version_assoc_count = db.session.execute(
                delete(DbCurrentPageVersionAssociation).filter(
                    DbCurrentPageVersionAssociation.page_id.in_(page_ids)
                )
            ).rowcount
        counts['current_page_version_associations'] = (
            current_version_assoc_count
        )

        # Delete page versions for pages of this site
        page_versions_count = 0
        if page_ids:
            page_versions_count = db.session.execute(
                delete(DbPageVersion).filter(DbPageVersion.page_id.in_(page_ids))
            ).rowcount
        counts['page_versions'] = page_versions_count

        # Delete pages for this site
        pages_count = db.session.execute(
            delete(DbPage).filter_by(site_id=site_id)
        ).rowcount
        counts['pages'] = pages_count

        # Delete news channel associations (junction table)
        news_channel_assoc_count = db.session.execute(
            delete(site_news_channels).where(
                site_news_channels.c.site_id == site_id
            )
        ).rowcount
        counts['news_channel_associations'] = news_channel_assoc_count

        # Delete site settings
        site_settings_count = db.session.execute(
            delete(DbSiteSetting).filter_by(site_id=site_id)
        ).rowcount
        counts['site_settings'] = site_settings_count

        # Finally, delete the site itself
        sites_count = db.session.execute(
            delete(DbSite).filter_by(id=site_id)
        ).rowcount
        counts['sites'] = sites_count

        db.session.commit()

        return {
            'success': True,
            'error': None,
            'counts': counts,
        }

    except Exception as e:
        db.session.rollback()
        return {
            'success': False,
            'error': str(e),
            'counts': {},
        }
