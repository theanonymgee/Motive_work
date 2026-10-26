"""``workmcp setup`` contract (design M5 §2.3, §12; stage 0): arguments, argument rules, result shape and
exit codes. The step bodies come in stage 1; until then ``apply``/``plan`` raise ``NotImplementedError`` and
the CLI exits 1 without creating anything.
"""
from pathlib import Path

import pytest

from workmcp import cli, setup

ABS = str(Path(__file__).resolve().parent)  # any absolute folder (the argument rules never touch it)


def parse(*argv):
    """The ``setup`` namespace exactly as ``cli.main`` builds it (``setup.main`` stubbed to capture it)."""
    seen = {}

    def capture(args):
        seen['args'] = args
        return 0

    real, setup.main = setup.main, capture
    try:
        assert cli.main(['setup', *argv]) == 0
    finally:
        setup.main = real
    return seen['args']


def test_cli_setup_arguments_map_to_options():
    options = setup.options_from_args(parse('--home', ABS, '--source', 'C:\\a', '--source', 'C:\\b', '--model',
                                            'download', '--clients', 'codex', '--scope', 'project', '--project', ABS,
                                            '--scan', '--index', '--yes'))
    assert options == setup.Options(home=ABS, sources=('C:\\a', 'C:\\b'), model='download', clients=('codex',),
                                    scope='project', project=ABS, scan=True, index=True, yes=True)
    defaults = setup.options_from_args(parse())
    assert defaults == setup.Options()
    assert defaults.clients == setup.CLIENTS and defaults.scope == 'user' and not defaults.upgrade
    assert setup.options_from_args(parse('--home', ABS, '--model-from', ABS)).model_from == ABS
    assert setup.options_from_args(parse('--home', ABS, '--upgrade')).upgrade is True


@pytest.mark.parametrize('changes, message', [
    ({'home': None}, '--home PATH is required'),
    ({'model': None}, '--model download|none or --model-from DIR is required'),
    ({'model_from': ABS}, 'exclude each other'),
    ({'model': 'latest'}, '--model must be one of download, none'),
    ({'model': None, 'model_from': 'relative\\bundle'}, '--model-from must be an absolute folder'),
    ({'scope': 'global'}, '--scope must be one of user, project'),
    ({'scope': 'project'}, '--scope project needs --project DIR'),
    ({'project': ABS}, '--project DIR needs --scope project'),
    ({'scope': 'project', 'project': 'proj'}, '--project must be an absolute folder'),
    ({'clients': ()}, '--clients must be'),
    ({'clients': ('claude', 'claude')}, '--clients must be'),
    ({'clients': ('claude', 'cursor')}, '--clients must be'),
])
def test_check_options_names_the_broken_rule(changes, message):
    base = {'home': ABS, 'model': 'none'}
    error = setup.check_options(setup.Options(**{**base, **changes}))
    assert error is not None and message in error


@pytest.mark.parametrize('options', [
    setup.Options(home=ABS, model='none'),
    setup.Options(home=ABS, model='download', sources=('C:\\x',), scan=True, index=True),
    setup.Options(home=ABS, model_from=ABS, scope='project', project=ABS, clients=('claude',)),
    setup.Options(home=ABS, upgrade=True),  # --upgrade needs no model choice (its other steps are skipped)
])
def test_check_options_accepts_valid_combinations(options):
    assert setup.check_options(options) is None


def test_result_shape_and_exit_codes_are_fixed():
    assert setup.RESULT_KEYS == ('exit_code', 'dry_run', 'home', 'steps')
    assert setup.STEPS == ('home', 'config', 'model', 'clients', 'doctor', 'scan', 'index', 'upgrade')
    assert setup.STEP_KEYS == ('status', 'changed', 'lines', 'detail')
    assert set(setup.STEP_DETAIL_KEYS) == set(setup.STEPS)
    assert (setup.EXIT_OK, setup.EXIT_FAILED, setup.EXIT_HOME, setup.EXIT_LOCKED, setup.EXIT_USAGE) == (0, 1, 2, 3, 4)
    assert setup.MODEL_CHOICES == ('download', 'none') and setup.SCOPES == ('user', 'project')


def test_cli_setup_exits_4_on_arguments_and_1_while_the_body_is_a_stub(tmp_path, capsys):
    home = tmp_path/'Motive_work'
    assert cli.main(['setup', '--model', 'none']) == 4
    assert '--home PATH is required' in capsys.readouterr().err
    assert cli.main(['setup', '--home', str(home), '--model', 'none', '--model-from', str(tmp_path)]) == 4
    assert 'exclude each other' in capsys.readouterr().err
    assert cli.main(['setup', '--home', str(home), '--model', 'none']) == 1
    out, err = capsys.readouterr()
    assert out == '' and 'stage 1' in err
    assert not home.exists()
    with pytest.raises(NotImplementedError):
        setup.plan(setup.Options(home=str(home), model='none'))
    assert not home.exists()
