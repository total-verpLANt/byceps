# genial-verplant: operator runbook

Site id `genial-verplant`. The id is party-neutral: the same site carries over from party to party. Each party gets its character from the party topic (see "Party topic" below), not from a redesign.

This file is not served. The reverse proxy maps only `static/` of a site (`/static_sites/genial-verplant/...` to `sites/genial-verplant/static/...`). Templates live in `template_overrides/`; BYCEPS finds them through the site id, so the id and the directory name must match exactly: `genial-verplant`.

Everything below is done in the admin UI or in the deployment config. Nothing in this directory creates database rows by itself.

## 0. Prerequisites

- The brand exists, and so do its shop, storefront, board and news channel (the site row only points at them).
- The party exists. Create it at `/parties/for_brand/<brand_id>/create`. The party title must end in `#<number>`, for example `genial verpLANt #37`: the number feeds the topic labels ("Thema der #37"). Without it the labels fall back to "Thema:".
- Same brand as the archive site `totalverplant-36` makes steps 4 and 5 easier: the admin can copy menus and pages between sites of one brand.

## 1. Database site row

Admin UI: `/sites/sites/create/for_brand/<brand_id>`.

| Field | Value |
|---|---|
| ID | `genial-verplant` (stored lower case, max 40 characters) |
| Title | `genial verpLANt` (unique, max 40 characters) |
| Server name | the public host name of the site (unique) |
| Party | the party created above |
| enabled | on when the site goes live; off keeps it dark |
| User registration open / User login open | as wanted |
| Board ID | the brand's board |
| Storefront ID | the brand's storefront |
| Use as intranet / Check in attendees on login | off, unless this party needs them |

Afterwards assign the news channel(s) at `/sites/sites/genial-verplant/news_channels/update`.

A disabled site answers every request with 404. To take the site offline again, switch `enabled` off at `/sites/sites/genial-verplant/update`.

Do not bind two sites that both ship `static/style/seating.css` to the same party unless the party setting `primary_party_site_id` names one of them. The admin seating plan takes the stylesheet from that site, and finds none when the choice is ambiguous.

## 2. `config.toml` entry

Add the site to `[apps].sites` and restart the processes that serve the sites:

```toml
[apps]
sites = [
  # ...existing sites...
  { server_name = "<public host name>", site_id = "genial-verplant" },
]
```

`server_name` must equal the server name of the database row. The reverse proxy needs a server block (and a certificate) for the new host name. The shared `/static_sites/` rule already serves this site's `static/`, so no per-site static location is needed.

## 3. Static files in this directory

| Path | Content |
|---|---|
| `static/style/owl.png` | the owl (header, picker, hero) |
| `static/style/favicon.ico` | favicon |
| `static/style/seating.css` | seat size for the seating plan |
| `static/style/sponsoren/` | 36 files: 12 sponsors with `_167.webp`, `_334.webp` and `_600x250.png` each |
| `static/behavior/chair-information.js` | chair information on the ticket page |
| `static/style/fonts/` | webfonts, see section 8 |

A new sponsor needs its three image files in `sponsoren/` and one entry in the sponsor list of `template_overrides/site/homepage/index.html`.

## 4. Navigation menu `main`

The header, the phone menu and the LAN '99 navigation strip all print the items of the menu named `main` (`get_nav_menu_items('main')`), in menu order. Each link carries `aria-current="page"` when the item's current-page id matches the page. The menu prints every item, including a link to `/`.

Admin UI: `/sites/navigation/for_site/genial-verplant`.

Preferred: copy the menu from the archive site, so the items match it exactly. `/sites/navigation/for_site/genial-verplant/copy`, source `totalverplant-36`. This works only while the new site has no menus and shares the brand.

By hand: create the menu at `/sites/navigation/for_site/genial-verplant/create` (name `main`, language `de`, not hidden), then add items. Example set:

| Label | Item type | Target |
|---|---|---|
| Event-Infos | page | `event-infos` |
| Zeitplan | page | `zeitplan` |
| Turniere | page | `turniere` |
| Sitzplan | view | Seating plan (`seating_plan`) |
| News | view | News (`news`) |
| Forum | view | Board (`board`) |
| FAQ | page | `faq` |
| Verein | page | `verein` |

Page items take their current-page id from the page (`page_<name>`); view items from the view type. URL items need both a target and a current-page id typed in.

## 5. Database pages

Nine pages carry the long-form content. Create them at `/pages/for_site/genial-verplant/create` (name, language `de`, URL path, title, empty header, body, not hidden). The bodies are the files of the same name in `backups/totalverplant-36-pages-handoff/` of the repository checkout (the newest generation: it contains the markup the content stylesheet is written for). `backups/totalverplant-36-pages/` holds the original export and `backups/totalverplant-36-pages-redesign/` the generation before the handoff round.

| Name | URL path | Suggested title | Body file |
|---|---|---|---|
| `agb` | `/agb` | Allgemeine Geschäftsbedingungen | `agb.html` |
| `privacy` | `/privacy` | Datenschutzerklärung | `privacy.html` |
| `event-infos` | `/event-infos` | Event-Infos | `event-infos.html` |
| `faq` | `/faq` | FAQ | `faq.html` |
| `gn` | `/gn` | Gaming Night | `gn.html` |
| `impressum` | `/impressum` | Impressum | `impressum.html` |
| `turniere` | `/turniere` | Turnierankündigungen | `turniere.html` |
| `verein` | `/verein` | Verein | `verein.html` |
| `zeitplan` | `/zeitplan` | Zeitplan | `zeitplan.html` |

Paste the file content into the body field unchanged. The nav items of section 4 reference these names.

Alternative: `/pages/for_site/genial-verplant/copy` copies pages straight from `totalverplant-36` (same brand). That copies whatever the archive site holds in the database today, which may differ from the files.

The bodies were written for party 36: edition labels, dates, prices and names in them need a read-through before go-live.

## 6. Homepage snippets (all optional)

Snippets live in the scope `site/genial-verplant`: list at `/snippets/for_scope/site/genial-verplant`, create at `/snippets/for_scope/site/genial-verplant/create`. Names are lower case, the language code is `de` (a snippet is looked up for the visitor's locale, so create it for every locale the site serves). A missing or empty snippet simply renders nothing.

Snippet bodies are evaluated as templates and printed unescaped. Treat `snippet.create` and snippet editing as trusted permissions.

Each snippet has a markup contract, because the homepage prints it into a fixed place.

### `homepage_venue`

Printed inside the hero's `<dl class="hero-meta">`, right after the "Wann" row. It must therefore be `<dt>`/`<dd>` pairs, with the `dd` classes `where` and `basics`. The `dt` text is read by screen readers only.

```html
<dt>Wo</dt><dd class="where">Halle Beispielstadt, Musterstraße 1, 12345 Beispielstadt</dd>
<dt>Eckdaten</dt><dd class="basics">240 Plätze · 1 Gbit/s · Verpflegung vor Ort</dd>
```

### `homepage_facts`

Rendered in the band "Auf einen Blick". Bring your own wrapper: `.facts` around `.fact` children, each with a `b` (the value) and a `span` (the label). The band is hidden while the snippet is empty.

```html
<div class="facts">
  <div class="fact"><b>240</b><span>Sitzplätze</span></div>
  <div class="fact"><b>3 Tage</b><span>Freitag bis Sonntag</span></div>
  <div class="fact"><b>1 Gbit/s</b><span>Netzwerk am Platz</span></div>
</div>
```

### `homepage_schedule`

Rendered in the band "Zeitplan". Bring your own wrapper: `.days` around `.day` children, each with an `h3` and an `ol` of `li` rows (a `time` and a text). The band is hidden while the snippet is empty.

```html
<div class="days">
  <div class="day">
    <h3>Freitag</h3>
    <ol>
      <li><time>16:00</time><span>Einlass und Aufbau</span></li>
      <li><time>20:00</time><span>Turnierstart</span></li>
    </ol>
  </div>
  <div class="day">
    <h3>Samstag</h3>
    <ol>
      <li><time>12:00</time><span>Finalrunden</span></li>
      <li><time>22:00</time><span>Siegerehrung</span></li>
    </ol>
  </div>
</div>
```

### `homepage`

Free HTML printed once at the very end of the homepage, below the sponsor band. No wrapper is required.

```html
<section class="band">
  <div class="band-head"><div><h2>Hinweis</h2></div></div>
  <p>Kurzer Text für alle Besucher.</p>
</section>
```

## 7. Party topic

A party topic gives one party its own name pun, colour and key art, in all three looks, with no code change. It is one data entry and one art file.

### How it behaves

- State `none`: no entry for the party, or the party has ended (`now >= party end`). The site shows its normal look.
- State `teaser`: from `teaser_at`. Only the announce date and the optional `teaser_text` render ("Das Thema wird am 1.8. verraten."). Nothing else of the entry reaches the page.
- State `on`: from `announce_at`. Name, pun, colour, art and the topic lines render, and the one-time reveal plays once per browser (storage key `tv-topic-seen-<party id>`).
- The data file and the art are templates and are never served as files, so nothing is fetchable before the reveal. View-source in the teaser state must show no topic name, colour value or art.
- The brand name stays „genial verpLANt“ in the page title, the brand link's accessible name and the owl's alt text, whatever the topic is.

### Entry shape

The data lives in `template_overrides/layout/_topics.html`, which ships as `{% set TOPICS = [] %}`. Put one dictionary per party into that list. Entries match on `party_id`.

```jinja
{% set TOPICS = [
  {
    'party_id': 'gv-NN',                      {# id of the party in the admin UI #}
    'name': 'Thema',                          {# shown in lines, labels, footer #}
    'pun': ['ver', 'THEMA', 'et'],            {# genial verTHEMAet; the middle part takes the topic colour #}
    'colour': '#a78bfa',
    'on_dark': '#c4b5fd',                     {# text colour on dark surfaces #}
    'on_light': '#6d28d9',                    {# text colour on the light surface #}
    'glow': 'rgba(167, 139, 250, .38)',
    'art': 'topics/3f9a2c71.svg.html',        {# neutral hash name, see below #}
    'teaser_at': (2027, 6, 1, 8, 0),          {# naive UTC #}
    'announce_at': (2027, 8, 1, 16, 0),       {# naive UTC = 18:00 CEST #}
    'teaser_text': none,                      {# optional #}
    'footer_line': none,                      {# optional #}
    'error_line': none,                       {# optional #}
    'og_image': none,                         {# optional, e.g. 'style/og/9c1e.png' #}
  },
] %}
```

Required keys: `party_id`, `name`, `pun`, `colour`, `on_dark`, `on_light`, `glow`, `art`, `teaser_at`, `announce_at`. Optional keys may be left out entirely: `teaser_text`, `footer_line`, `error_line`, `og_image`.

- `pun` has exactly three parts. Together they must stay within 11 characters, so the wordmark fits the header.
- `footer_line` replaces the footer sentence ("Thema der #37: ..."). `error_line` adds one line on the 404 page.
- The four colour values are written into an inline style block as they are. Use plain hex or `rgba()` literals only.
- `og_image` is a path inside this site's `static/` (it renders through the site-file URL as an absolute URL). That file is publicly fetchable as soon as it is deployed, so give it a neutral hash name and deploy it only at announce time.

### Dates are naive UTC

`teaser_at` and `announce_at` are tuples `(year, month, day, hour, minute)` in UTC, compared with the server's naive UTC clock. 18:00 CEST is 16:00 UTC; 18:00 CET is 17:00 UTC. The teaser prints day and month from the `announce_at` tuple ("1.8."), so announce at a daytime hour: near midnight UTC the printed date could be off by one against local time.

### Art

One art file per entry: `template_overrides/topics/<hash>.svg.html`.

- The name is a neutral hash, never the topic word. The `art` key holds the path relative to `template_overrides/`.
- The file is one `<svg viewBox="..." aria-hidden="true">` with no fixed width or height. Paint it with `var(--tp)` (topic colour), `var(--tp-ink)`, `var(--tp-acc)` and `var(--tp-shadow)` so it follows each look.
- It never goes under `static/`: files there are served to everyone.

### Colour rules and check

Every entry must satisfy:

- `on_dark`: contrast of at least 4.5:1 against `--bg`, `--s1` and `--s2` of Owly dark, Clan 2003 and LAN '99.
- `on_light`: at least 4.5:1 against the same three surfaces of Owly light, and at least 3:1 against `#c0c0c0`.
- Hue: at least 20 degrees away from the brand orange `#f39200` and at least 15 degrees away from the danger colour of both themes.
- `art` starts with `topics/` and no file under `static/` shares its name.

Run from the repository root:

```
.venv/bin/python -m pytest tests/unit/sites/genial_verplant/test_topic.py -q
```

The same command also checks the three topic states and that no teaser output leaks entry data.

### Per-party checklist

1. Party exists, bound to the site row, title ends in `#<number>`.
2. One new entry in `_topics.html` (older entries may stay: each only matches its own party id).
3. One art file `topics/<hash>.svg.html`, neutral hash name.
4. Optional og image under `static/style/og/`, deployed at announce time only.
5. Run the colour check above.
6. Deploy and restart. On staging, set `teaser_at` and `announce_at` to times in the past or future to see the teaser and the reveal in all three looks, then put the real dates back.

## 8. Fonts and licences

All fonts are self-hosted. Nothing is loaded from another host.

Layout:

- `static/style/fonts/fonts.css` declares the faces.
- `static/style/fonts/<family>/<file>.woff2` holds the files. Five families:

| Family (directory) | Files |
|---|---|
| Reddit Sans Condensed (`reddit-sans-condensed`) | `reddit-sans-condensed-latin-{500,700,800}.woff2` (3) |
| Reddit Sans (`reddit-sans`) | `reddit-sans-latin-{400,500,600,700}.woff2` (4) |
| JetBrains Mono (`jetbrains-mono`) | `jetbrains-mono-latin-ext-{400,500,700}.woff2` (3) |
| Black Ops One (`black-ops-one`) | `black-ops-one-latin-400.woff2` (1) |
| VT323 (`vt323`) | `vt323-latin-400.woff2` (1) |

- The licence texts (SIL Open Font License) live outside `static/`, so they are versioned but not served: `FONT-LICENSES/<family>-OFL.txt` at the site root, next to `static/`.
- LAN '99 uses system fonts and needs no files.

Before go-live, run from `sites/genial-verplant/`:

```
ls static/style/fonts/*/*.woff2 | wc -l          # must print 12
for f in static/style/fonts/*/*.woff2; do head -c4 "$f"; echo; done | sort -u   # must print only wOF2
ls FONT-LICENSES/*.txt                           # one OFL text per family
```

Twelve is 3 (JetBrains Mono) plus 9 (the other four families). A missing file makes the browser fall back to a system font without any error. The JetBrains Mono files were carried over from the archive site, which shipped no licence text for them: add `FONT-LICENSES/jetbrains-mono-OFL.txt` if it is not there yet.

## 9. Staging QA matrix

Run on staging before the production switch. Anonymous first, then logged in as a ticket holder.

Reachability:

```
curl -s -o /dev/null -w '%{http_code}\n' https://<staging host>/                                   # 200
curl -s -o /dev/null -w '%{http_code}\n' https://<staging host>/static_sites/genial-verplant/style/owl.png   # 200
```

Looks, widths and pages. Check every cell at 360 px and at 1280 px:

| Look | Pages |
|---|---|
| Owly dark | home, news, board, tickets/mine, seating, shop order, dashboard, lan-tournaments index, a bracket page, a database page |
| Owly light | same list |
| Clan 2003 | same list |
| LAN '99 | same list |

Rules for the matrix:

- Retro looks change header, navigation, footer, picker and the homepage only. Every other page keeps its content area in Owly colours in every look. The tournament pages stay dark in every look and theme.
- A first visit is dark, whatever the operating system prefers. Light applies only after the toggle.
- With JavaScript off: Owly dark, no picker, the phone menu still opens (it is a `<details>`).
- Nothing overflows horizontally at 360 px. Every button, link and control is at least 44 px tall on a touch device.

Eggs (all start locked; Owly is always free). Reload after each to see the state persist:

| Egg | How | Unlocks |
|---|---|---|
| Owl | click the owl in the header five times | Clan 2003 |
| Year | click the year in the footer copyright line (it counts back to 1999) | LAN '99 |
| Konami | up up down down left right left right B A | LAN '99 |
| Console | press the `^` key (the key left of `1` on a German keyboard), type `sv_cheats 1`, Enter | both |

Browser storage keys involved: `tv-era`, `tv-theme`, `tv-unlocked`, `tv-new`, `tv-offer`, and `tv-topic-seen-<party id>`. Clear them to repeat a first visit.

Motion: with the operating system's "reduce motion" on, marquee, cursor blink, LED pulse, flashes, toast, picker spring, egg wobble and the topic reveal all stand still.

Topic (needs a temporary entry for the staging party, see section 7):

- Teaser state: the teaser line shows in all three looks (the noise screen in the hero only in Owly and Clan 2003); view-source contains no topic name, colour value or art.
- Revealed state: pun in header and hero, name in footer and hero line, art in the hero screen (Owly, Clan 2003) and in the framed box (LAN '99); the reveal plays once, then not again on reload.
- Put the real dates back afterwards.

Pages and data:

- All nine database pages open and are readable in Owly dark and light.
- The homepage bands appear for the snippets that exist, and are absent for the ones that do not.
- The ticket meter, tournament cards and the news list show real data.
- A made-up URL shows the site's own 404 page.
