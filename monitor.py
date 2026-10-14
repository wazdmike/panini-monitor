import argparse
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
SEARCH_URL = 'https://panini.com.br/catalogsearch/result/?q=bleach%20remix'
STATE = Path('prices.json')
PENDING = Path('pending_alerts.json')
SEEN = Path('availability.json')
VOLUMES = Path('volume_status.json')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'BleachRemixPaniniMonitor/2.0 (personal monitoring)', 'Accept-Language': 'pt-BR,pt;q=0.9'})


def read_json(path):
    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8").strip()

        if not content:
            return {}

        return json.loads(content)

    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"WARNING: JSON inválido em {path}")
        return {}
    
def write_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def relevant(title):
    return bool(re.search(r'\bbleach\s+remix\b', re.sub(r'[^a-z0-9]+', ' ', title.lower())))


def parse_price(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace('R$', '').replace('\xa0', '')
    match = re.search(r'\d[\d.,]*', s)
    if not match:
        return None
    s = match.group(0)
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    elif s.count('.') > 1:
        s = s.replace('.', '')
    try:
        value = Decimal(s)
        return value if Decimal('1') <= value <= Decimal('1000') else None
    except InvalidOperation:
        return None


def permitted(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (host == 'panini.com.br' or host.endswith('.panini.com.br'))


def canonical(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip('/'), '', '', ''))


def stock_status(card):
    """True/False only with explicit evidence; None when unknown."""
    classes = ' '.join(card.get('class', [])).lower()
    text = card.get_text(' ', strip=True).lower()
    if 'out-of-stock' in classes or 'out of stock' in text or 'esgotado' in text or 'indisponível' in text:
        return False
    if 'in-stock' in classes or 'em estoque' in text or 'disponível para compra' in text:
        return True
    for el in card.select('[itemprop="availability"], link[itemprop="availability"]'):
        value = (el.get('href', '') + ' ' + el.get('content', '')).lower()
        if 'outofstock' in value or 'soldout' in value:
            return False
        if 'instock' in value:
            return True
    return None


def volume_number(title):
    match = re.search(r'\bbleach\s+remix\s+vol\.?\s*(\d{1,2})\b', title, re.I)
    if not match:
        return None
    n = int(match.group(1))
    return n if 1 <= n <= 26 else None


def fetch_search_pages():
    """Search discovery is supplementary: a volume can exist outside search results."""
    found = {}
    for page in (1, 2):
        url = SEARCH_URL + ('&p=2' if page == 2 else '')
        try:
            response = SESSION.get(url, timeout=25)
            logging.info('Busca Panini página %d HTTP %s', page, response.status_code)
            response.raise_for_status()
            if not permitted(response.url):
                raise ValueError('Redirecionamento externo')
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.select('.product-item, .product-card, li.product'):
                title_el = card.select_one('.product-item-link, .product-item-name, .product-name')
                link = card.select_one('a.product-item-link[href], a.product-item-photo[href]')
                if not title_el or not link:
                    continue
                number = volume_number(title_el.get_text(' ', strip=True))
                link_url = canonical(urljoin(url, link['href']))
                if number is not None and permitted(link_url):
                    found[number] = link_url
            logging.info('Busca página %d: %d volumes identificados até agora', page, len(found))
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Busca página %d indisponível: %s', page, type(exc).__name__)
        time.sleep(0.7)
    return found


def product_state(soup):
    """Only examine the main product area; related products may say 'out of stock'."""
    main = soup.select_one('.product-info-main')
    if main is None:
        return 'unknown'
    text = main.get_text(' ', strip=True).lower()
    # Preorder must take precedence over 'Em estoque' (both occur on vol. 14).
    if re.search(r'pr[eé][- ]?venda', text, re.I):
        return 'preorder'
    if 'fora de estoque' in text or 'produto indisponível' in text:
        return 'out_of_stock'
    if re.search(r'\bem estoque\b', text):
        return 'in_stock'
    return 'unknown'


def product_price(soup):
    main = soup.select_one('.product-info-main')
    if main is None:
        return None
    selectors = (
        '.special-price [data-price-amount]',
        '[data-price-type="finalPrice"] [data-price-amount]',
        '.special-price .price',
        '[data-price-type="finalPrice"] .price',
        '.price-box .price',
    )
    for selector in selectors:
        el = main.select_one(selector)
        if el:
            result = parse_price(el.get('data-price-amount') or el.get_text(' ', strip=True))
            if result is not None:
                return result
    return None


def fetch_panini(previous, seen):
    discovered = fetch_search_pages()
    # Preserve known edition URLs (some have suffixes, e.g. -abler001r5).
    known = {}
    for source in (previous, seen):
        for url, item in source.items():
            if not isinstance(item, dict) or not permitted(url):
                continue
            number = volume_number(item.get('title', ''))
            if number is not None:
                known[number] = url
    items = {}
    succeeded = 0
    statuses = {}
    for number in range(1, 27):
        url = discovered.get(number) or known.get(number) or f'https://panini.com.br/bleach-remix-vol-{number}'
        try:
            response = SESSION.get(url, timeout=25)
            if response.status_code in (404, 410):
                logging.info('Volume %02d: página inexistente (HTTP %s)', number, response.status_code)
                statuses[str(number)] = {'status': 'not_found', 'url': url}
                continue
            response.raise_for_status()
            if not permitted(response.url):
                raise ValueError('Redirecionamento externo')
            soup = BeautifulSoup(response.text, 'html.parser')
            heading = soup.select_one('h1.page-title') or soup.select_one('h1')
            title = heading.get_text(' ', strip=True) if heading else ''
            if volume_number(title) != number:
                logging.warning('Volume %02d: página sem título de produto correspondente', number)
                continue
            status = product_state(soup)
            price = product_price(soup)
            key = canonical(response.url)
            if price is None:
                logging.warning('Volume %02d: preço não identificado; estado de estoque preservado', number)
            items[key] = {'shop': 'Panini', 'title': title[:180], 'price': str(price) if price is not None else None,
                          'url': key, 'status': status, 'stock': status == 'in_stock' if status != 'unknown' else None}
            statuses[str(number)] = {'status': status, 'url': key}
            succeeded += 1
            logging.info('Volume %02d: %s | preço %s', number, status, str(price) if price is not None else 'desconhecido')
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Volume %02d: consulta falhou (%s)', number, type(exc).__name__)
        time.sleep(0.8)
    logging.info('Panini: %d páginas individuais verificadas com sucesso', succeeded)
    return items, statuses


def money(value):
    return f'R$ {value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def send_telegram(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        logging.warning('Secrets TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausentes')
        return False
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'disable_web_page_preview': True},
            timeout=20,
        )
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError('Telegram rejeitou a mensagem')
        return True
    except (requests.RequestException, ValueError) as exc:
        logging.error('Falha ao enviar Telegram (%s)', type(exc).__name__)
        return False


def notify(item, old):
    new = Decimal(item['price'])
    return send_telegram(
        f'📉 Bleach Remix caiu de preço!\n'
        f"Loja: Panini\nProduto: {item['title']}\n"
        f'Antes: {money(old)}\nAgora: {money(new)}\n'
        f'Economia: {money(old - new)}\n{item["url"]}'
    )


def main():
    previous = read_json(STATE)
    seen = read_json(SEEN)
    pending = read_json(PENDING)
    saved_volumes = read_json(VOLUMES)
    updated_prices = dict(previous)
    updated_seen = dict(seen)

    # Migra o histórico anterior para a identificação por número de volume.
    # A primeira execução cria uma referência, sem mandar alertas retroativos.
    if not saved_volumes:
        for url, entry in seen.items():
            if not isinstance(entry, dict):
                continue
            number = volume_number(entry.get('title', ''))
            if number is None:
                continue
            status = entry.get('status')
            if status not in ('in_stock', 'out_of_stock', 'preorder'):
                stock = entry.get('stock')
                status = 'in_stock' if stock is True else 'out_of_stock' if stock is False else 'unknown'
            saved_volumes[str(number)] = {'status': status, 'url': url}
        logging.info('Inicializando histórico de volumes sem alertas retroativos')
        bootstrap = True
    else:
        bootstrap = False

    # Mantém alertas pendentes de estoque e preço para tentar novamente em caso de falha.
    pending = {key: alert for key, alert in pending.items()
               if isinstance(alert, dict) and alert.get('kind') in ('stock_change', 'price_drop')}

    items, statuses = fetch_panini(previous, seen)
    next_volumes = dict(saved_volumes)
    valid = {'not_found', 'in_stock', 'out_of_stock', 'preorder'}
    for number in range(1, 27):
        key = str(number)
        current = statuses.get(key)
        if current is None:
            # Falha de rede, 403, erro de parser etc.: não inferir mudança.
            continue
        status = current['status']
        old = saved_volumes.get(key, {})
        old_status = old.get('status') if isinstance(old, dict) else None
        if status == 'unknown':
            # Não substituir um estado confiável por uma classificação inconclusiva.
            if key not in next_volumes:
                next_volumes[key] = current
            continue
        next_volumes[key] = current
        if not bootstrap and old_status in valid and old_status != status:
            # Qualquer transição real: inclusive 404 -> estoque/pré-venda,
            # em estoque -> esgotado e pré-venda -> em estoque.
            pending['stock:' + key] = {
                'kind': 'stock_change', 'number': number,
                'old_status': old_status, 'status': status,
                'url': current['url'],
            }
        elif 'stock:' + key in pending:
            alert = pending['stock:' + key]
            if alert.get('status') != status:
                del pending['stock:' + key]

    for url, item in items.items():
        # Compara com o histórico persistido; não notifica na primeira observação.
        previous_item = previous.get(url)
        new_raw = item.get('price')
        old_raw = previous_item.get('price') if isinstance(previous_item, dict) else None
        alert_key = 'price:' + url
        try:
            new_price = Decimal(str(new_raw)) if new_raw is not None else None
            old_price = Decimal(str(old_raw)) if old_raw is not None else None
            if new_price is not None and old_price is not None and new_price < old_price:
                pending[alert_key] = {
                    'kind': 'price_drop', 'url': url, 'title': item['title'],
                    'old_price': str(old_price), 'new_price': str(new_price),
                }
            elif alert_key in pending:
                # Não envia uma queda antiga se o preço já voltou a subir.
                if new_price is not None and new_price >= Decimal(str(pending[alert_key]['old_price'])):
                    del pending[alert_key]
        except (InvalidOperation, TypeError, ValueError, KeyError):
            logging.warning('Preço inválido para comparação: %s', url)
        updated_prices[url] = item
        if item['status'] != 'unknown':
            updated_seen[url] = {'status': item['status'], 'stock': item['stock'], 'title': item['title']}

    labels = {
        'not_found': 'Página ainda não cadastrada',
        'in_stock': 'Em estoque',
        'out_of_stock': 'Sem estoque',
        'preorder': 'Pré-venda',
    }
    for key, alert in list(pending.items()):
        if alert.get('kind') == 'price_drop':
            try:
                old_price = Decimal(str(alert['old_price']))
                new_price = Decimal(str(alert['new_price']))
                if old_price <= new_price:
                    del pending[key]
                    continue
                message = (f'📉 Bleach Remix caiu de preço!\n'
                           f'Produto: {alert["title"]}\n'
                           f'Antes: {money(old_price)}\nAgora: {money(new_price)}\n'
                           f'Economia: {money(old_price - new_price)}\n{alert["url"]}')
                if send_telegram(message):
                    del pending[key]
            except (KeyError, InvalidOperation, TypeError, ValueError):
                logging.warning('Alerta de preço inválido removido: %s', key)
                del pending[key]
            continue
        try:
            number = int(alert['number'])
            before = alert['old_status']
            after = alert['status']
            if before not in labels or after not in labels or not 1 <= number <= 26:
                raise ValueError('Alerta inválido')
            # Descarta alerta obsoleto caso a disponibilidade tenha revertido.
            if next_volumes.get(str(number), {}).get('status') != after:
                del pending[key]
                continue
            message = (f'📦 Bleach Remix Vol. {number:02d} — alteração na Panini!\n'
                       f'Antes: {labels[before]}\nAgora: {labels[after]}\n'
                       f'{alert["url"]}')
            if send_telegram(message):
                del pending[key]
        except (KeyError, TypeError, ValueError):
            logging.warning('Alerta inválido removido: %s', key)
            del pending[key]

    write_json(STATE, updated_prices)
    write_json(SEEN, updated_seen)
    write_json(VOLUMES, next_volumes)
    write_json(PENDING, pending)
    counts = {name: sum(v.get('status') == name for v in next_volumes.values() if isinstance(v, dict))
              for name in ('in_stock', 'out_of_stock', 'preorder', 'not_found')}
    logging.info('Volumes rastreados: %d/26 | Estoque: %d | Esgotados: %d | Pré-venda: %d | Não cadastrados: %d | Alertas pendentes: %d',
                 len(next_volumes), counts['in_stock'], counts['out_of_stock'], counts['preorder'], counts['not_found'], len(pending))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--test-telegram', action='store_true', help='Envia uma mensagem de teste, sem consultar preços')
    args = parser.parse_args()
    if args.test_telegram:
        if not send_telegram('🧪 Teste Bleach Remix Monitor\n✅ Bot conectado!\n📚 Monitoramento da Panini configurado.'):
            raise SystemExit(1)
        logging.info('Teste Telegram enviado com sucesso')
    else:
        main()
