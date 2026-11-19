"""Anonymise a battle before sharing it (demo pages, showcase videos)."""
import json


def anonymise(data, hide_me=False):
    """Other players' names -> 'Ally 3' / 'Enemy 5', clan tags and the chat (it carries account ids) dropped, in place.
    Your own name stays unless hide_me ('You'). Returns the strings that must not appear in the output any more."""
    secrets = set()
    counters = {'ally': 0, 'enemy': 0}
    for s in sorted(data['ships'], key=lambda s: (s['relation'] != 'self', s['relation'], s['id'])):
        if s.get('clan'):
            secrets.add(s['clan'])
        s['clan'] = ''
        if s['relation'] == 'self':
            if hide_me:
                secrets.add(s['name'])
                s['name'] = data['meta']['player'] = 'You'
            continue
        secrets.add(s['name'])
        side = 'ally' if s['relation'] == 'ally' else 'enemy'
        counters[side] += 1
        s['name'] = f'{side.capitalize()} {counters[side]}'
    for c in data['chat']:
        secrets.add(str(c[1]))
    data['chat'] = []
    return {x for x in secrets if x and len(x) > 2}


def leaks(secrets, text):
    """The secrets still found in text (as they would appear inside JSON)."""
    return sorted(x for x in secrets if json.dumps(x)[1:-1] in text)
