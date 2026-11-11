#!/usr/bin/env python3
"""Run the actual program with fake credentials and a mocked transport.

This demo never makes a network request and does not use real API keys.
"""
import io
import os
import sys
from unittest import mock

import collaborate


def simulated_provider(url, headers, payload, timeout):
    if url == 'https://api.anthropic.com/v1/messages':
        return {'id': 'simulated-response', 'model': 'simulated-model',
                'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': '17 * 23 = 391'}]}
    raise TimeoutError('simulated transport failure')


def main():
    fake_environment = {'CLAUDE_MODEL': 'simulated-claude', 'GROK_MODEL': 'simulated-grok',
                        'COLLAB_ANTHROPIC_KEY': 'fake-key-a', 'COLLAB_XAI_KEY': 'fake-key-b'}
    with mock.patch.dict(os.environ, fake_environment, clear=True), \
         mock.patch.object(sys, 'stdin', io.StringIO('17 * 23 = ?')), \
         mock.patch.object(collaborate, 'request_json', side_effect=simulated_provider):
        return collaborate.main(['--confirm-paid', '--stdin-utf8'])


if __name__ == '__main__':
    sys.exit(main())
