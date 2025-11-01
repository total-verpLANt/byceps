"""
byceps.cli.command.delete_brand
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Delete a brand and all related data.

:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import click
from flask.cli import with_appcontext
from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.board.dbmodels.board import DbBoard
from byceps.services.brand import brand_service
from byceps.services.brand.dbmodels import (
    DbBrand,
    DbBrandCurrentParty,
    DbBrandNewsletterList,
    DbBrandSetting,
)
from byceps.services.brand.models import BrandID
from byceps.services.consent.dbmodels import DbConsentBrandRequirement
from byceps.services.email.dbmodels import DbEmailConfig
from byceps.services.gallery.dbmodels import DbGallery
from byceps.services.news.dbmodels import DbNewsChannel, DbNewsItem
from byceps.services.orga.dbmodels import DbOrgaFlag
from byceps.services.party.dbmodels import DbParty
from byceps.services.shop.shop.dbmodels import DbShop
from byceps.services.site.dbmodels import DbSite
from byceps.services.user_badge.dbmodels import DbBadge


@click.command()
@click.argument('brand_id')
@click.option(
    '--force',
    is_flag=True,
    help='Skip confirmation prompt',
)
@click.option(
    '--nullify-badges',
    is_flag=True,
    help='Set brand_id to NULL for badges instead of blocking',
)
@with_appcontext
def delete_brand(brand_id: BrandID, force: bool, nullify_badges: bool) -> None:
    """Delete a brand and all related data."""
    brand = brand_service.find_brand(brand_id)

    if brand is None:
        click.secho(f'Brand "{brand_id}" not found.', fg='red')
        return

    # Get statistics before deletion
    stats = _gather_statistics(brand_id)

    # Display what will be deleted
    click.echo(f'\nBrand: {brand.title} ({brand_id})')
    click.echo('=' * 60)

    if _has_any_data(stats):
        _display_statistics(stats)
    else:
        click.secho(
            f'Brand "{brand_id}" has no related data.',
            fg='yellow',
        )

    # Check for blockers
    blockers = _check_blockers(brand_id, nullify_badges)
    if blockers:
        click.echo()
        click.secho(
            'Cannot delete brand due to the following issues:', fg='red'
        )
        for blocker in blockers:
            click.echo(f'  - {blocker}')
        return

    # Confirm deletion unless --force is used
    if not force:
        click.echo()
        if nullify_badges and stats['badges_with_brand'] > 0:
            click.echo(
                f'Note: {stats["badges_with_brand"]} badge(s) '
                'will have their brand_id set to NULL.'
            )
        if not click.confirm(
            'Do you want to delete this brand and all related data?'
        ):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    # Perform deletion
    click.echo('\nDeleting brand and related data...')
    result = _delete_brand_data(brand_id, nullify_badges)

    if result['success']:
        click.echo()
        click.secho('Successfully deleted:', fg='green')
        for key, count in sorted(result['counts'].items()):
            if count > 0:
                click.echo(f'  - {count} {key}')
    else:
        click.echo()
        click.secho(f'Error: {result["error"]}', fg='red')


def _gather_statistics(brand_id: BrandID) -> dict[str, int]:
    """Gather statistics about brand-related data."""
    return {
        'sites': _count_sites_for_brand(brand_id),
        'parties': _count_parties_for_brand(brand_id),
        'boards': _count_boards_for_brand(brand_id),
        'shops': _count_shops_for_brand(brand_id),
        'news_channels': _count_news_channels_for_brand(brand_id),
        'news_items': _count_news_items_for_brand(brand_id),
        'galleries': _count_galleries_for_brand(brand_id),
        'badges_with_brand': _count_badges_with_brand(brand_id),
        'brand_settings': _count_brand_settings(brand_id),
        'brand_newsletter_lists': _count_brand_newsletter_lists(brand_id),
        'brand_current_party': _count_brand_current_party(brand_id),
        'orga_flags': _count_orga_flags(brand_id),
        'consent_requirements': _count_consent_requirements(brand_id),
        'email_configs': _count_email_configs(brand_id),
    }


def _has_any_data(stats: dict[str, int]) -> bool:
    """Check if there is any related data."""
    return any(count > 0 for count in stats.values())


def _display_statistics(stats: dict[str, int]) -> None:
    """Display statistics about what will be deleted or blocks deletion."""
    deletable_keys = {
        'brand_settings',
        'brand_newsletter_lists',
        'brand_current_party',
        'orga_flags',
        'consent_requirements',
        'email_configs',
        'badges_with_brand',
    }

    blocker_keys = {
        'sites',
        'parties',
        'boards',
        'shops',
        'news_channels',
        'news_items',
        'galleries',
    }

    # Display blockers first
    blocker_stats = {k: v for k, v in stats.items() if k in blocker_keys and v > 0}
    if blocker_stats:
        click.echo('\nEntities that block deletion:')
        for key, count in sorted(blocker_stats.items()):
            click.echo(f'  - {key}: {count}')

    # Display deletable data
    deletable_stats = {
        k: v for k, v in stats.items() if k in deletable_keys and v > 0
    }
    if deletable_stats:
        click.echo('\nRelated data that will be deleted:')
        for key, count in sorted(deletable_stats.items()):
            click.echo(f'  - {key}: {count}')


def _check_blockers(brand_id: BrandID, nullify_badges: bool) -> list[str]:
    """Check for conditions that prevent deletion."""
    blockers = []

    # Check for sites
    sites_count = _count_sites_for_brand(brand_id)
    if sites_count > 0:
        blockers.append(
            f'{sites_count} site(s) belong to this brand. '
            'Delete or reassign sites first.'
        )

    # Check for parties
    parties_count = _count_parties_for_brand(brand_id)
    if parties_count > 0:
        blockers.append(
            f'{parties_count} party/parties belong to this brand. '
            'Delete or reassign parties first.'
        )

    # Check for boards
    boards_count = _count_boards_for_brand(brand_id)
    if boards_count > 0:
        blockers.append(
            f'{boards_count} board(s) belong to this brand. '
            'Delete or reassign boards first.'
        )

    # Check for shops
    shops_count = _count_shops_for_brand(brand_id)
    if shops_count > 0:
        blockers.append(
            f'{shops_count} shop(s) belong to this brand. '
            'Delete or reassign shops first.'
        )

    # Check for news channels
    news_channels_count = _count_news_channels_for_brand(brand_id)
    if news_channels_count > 0:
        blockers.append(
            f'{news_channels_count} news channel(s) belong to this brand. '
            'Delete or reassign news channels first.'
        )

    # Check for news items
    news_items_count = _count_news_items_for_brand(brand_id)
    if news_items_count > 0:
        blockers.append(
            f'{news_items_count} news item(s) belong to this brand. '
            'Delete news items first.'
        )

    # Check for galleries
    galleries_count = _count_galleries_for_brand(brand_id)
    if galleries_count > 0:
        blockers.append(
            f'{galleries_count} gallery/galleries belong to this brand. '
            'Delete or reassign galleries first.'
        )

    # Check for badges with brand_id
    if not nullify_badges:
        badges_count = _count_badges_with_brand(brand_id)
        if badges_count > 0:
            blockers.append(
                f'{badges_count} badge(s) are associated with this brand. '
                'Use --nullify-badges to set their brand_id to NULL.'
            )

    return blockers


def _count_sites_for_brand(brand_id: BrandID) -> int:
    """Count sites for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbSite.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_parties_for_brand(brand_id: BrandID) -> int:
    """Count parties for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbParty.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_boards_for_brand(brand_id: BrandID) -> int:
    """Count boards for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbBoard.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_shops_for_brand(brand_id: BrandID) -> int:
    """Count shops for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbShop.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_news_channels_for_brand(brand_id: BrandID) -> int:
    """Count news channels for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbNewsChannel.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_news_items_for_brand(brand_id: BrandID) -> int:
    """Count news items for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbNewsItem.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_galleries_for_brand(brand_id: BrandID) -> int:
    """Count galleries for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbGallery.id)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_badges_with_brand(brand_id: BrandID) -> int:
    """Count badges associated with this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbBadge.slug)).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_brand_settings(brand_id: BrandID) -> int:
    """Count brand settings."""
    return (
        db.session.scalar(
            select(db.func.count(DbBrandSetting.brand_id)).filter_by(
                brand_id=brand_id
            )
        )
        or 0
    )


def _count_brand_newsletter_lists(brand_id: BrandID) -> int:
    """Count brand newsletter list associations."""
    return (
        db.session.scalar(
            select(db.func.count(DbBrandNewsletterList.brand_id)).filter_by(
                brand_id=brand_id
            )
        )
        or 0
    )


def _count_brand_current_party(brand_id: BrandID) -> int:
    """Count brand current party association."""
    return (
        db.session.scalar(
            select(db.func.count(DbBrandCurrentParty.brand_id)).filter_by(
                brand_id=brand_id
            )
        )
        or 0
    )


def _count_orga_flags(brand_id: BrandID) -> int:
    """Count orga flags for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbOrgaFlag.brand_id)).filter_by(
                brand_id=brand_id
            )
        )
        or 0
    )


def _count_consent_requirements(brand_id: BrandID) -> int:
    """Count consent requirements for this brand."""
    return (
        db.session.scalar(
            select(
                db.func.count(DbConsentBrandRequirement.brand_id)
            ).filter_by(brand_id=brand_id)
        )
        or 0
    )


def _count_email_configs(brand_id: BrandID) -> int:
    """Count email configs for this brand."""
    return (
        db.session.scalar(
            select(db.func.count(DbEmailConfig.brand_id)).filter_by(
                brand_id=brand_id
            )
        )
        or 0
    )


def _delete_brand_data(brand_id: BrandID, nullify_badges: bool) -> dict:
    """Delete all brand-related data."""
    counts = {}

    try:
        # Nullify badges' brand_id if requested
        if nullify_badges:
            badges_nullified = db.session.execute(
                db.update(DbBadge)
                .where(DbBadge.brand_id == brand_id)
                .values(brand_id=None)
            ).rowcount
            counts['badges_nullified'] = badges_nullified

        # Delete orga flags
        orga_flags_count = db.session.execute(
            delete(DbOrgaFlag).filter_by(brand_id=brand_id)
        ).rowcount
        counts['orga_flags'] = orga_flags_count

        # Delete consent requirements
        consent_requirements_count = db.session.execute(
            delete(DbConsentBrandRequirement).filter_by(brand_id=brand_id)
        ).rowcount
        counts['consent_requirements'] = consent_requirements_count

        # Delete email configs
        email_configs_count = db.session.execute(
            delete(DbEmailConfig).filter_by(brand_id=brand_id)
        ).rowcount
        counts['email_configs'] = email_configs_count

        # Delete brand current party association
        brand_current_party_count = db.session.execute(
            delete(DbBrandCurrentParty).filter_by(brand_id=brand_id)
        ).rowcount
        counts['brand_current_party'] = brand_current_party_count

        # Delete brand newsletter lists
        brand_newsletter_lists_count = db.session.execute(
            delete(DbBrandNewsletterList).filter_by(brand_id=brand_id)
        ).rowcount
        counts['brand_newsletter_lists'] = brand_newsletter_lists_count

        # Delete brand settings
        brand_settings_count = db.session.execute(
            delete(DbBrandSetting).filter_by(brand_id=brand_id)
        ).rowcount
        counts['brand_settings'] = brand_settings_count

        # Finally, delete the brand itself
        brands_count = db.session.execute(
            delete(DbBrand).filter_by(id=brand_id)
        ).rowcount
        counts['brands'] = brands_count

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
