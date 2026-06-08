import os

import jinja2
import pytest

from _ravnar.utils import TemplateRenderError, render_template, render_template_context


class TestRenderTemplate:
    def test_basic_math(self):
        assert render_template("{{ 7 * 7 }}", {}) == "49"

    def test_env_var_access(self, mocker):
        mocker.patch.dict(os.environ, {"TEST_HOME": "/home/test"})
        assert render_template("{{ TEST_HOME }}", dict(os.environ)) == "/home/test"

    def test_dunder_access_raises_security_error(self):
        with pytest.raises(jinja2.exceptions.SecurityError):
            render_template("{{ config.__class__ }}", {"config": "value"})

    def test_self_dunder_access_raises_security_error(self):
        with pytest.raises(jinja2.exceptions.SecurityError):
            render_template("{{ ''.__class__.__mro__ }}", {})

    def test_strict_undefined_raises_on_missing_var(self):
        with pytest.raises(jinja2.exceptions.UndefinedError):
            render_template("{{ MISSING_VAR }}", {})

    def test_strict_undefined_default_filter(self):
        assert render_template('{{ MISSING_VAR | default("fallback") }}', {}) == "fallback"

    def test_strict_undefined_is_defined_test(self):
        assert render_template("{% if MISSING_VAR is defined %}yes{% else %}no{% endif %}", {}) == "no"

    def test_strict_undefined_conditional_access_raises(self):
        with pytest.raises(jinja2.exceptions.UndefinedError):
            render_template("{{ VAR if VAR else 'x' }}", {})

    def test_dict_key_rendering(self):
        assert render_template("{{ KEY }}", {"KEY": "value"}) == "value"

    def test_nested_dict(self):
        result = render_template({"key": "{{ VAL }}"}, {"VAL": "v"})
        assert result == {"key": "v"}

    def test_nested_list(self):
        result = render_template(["{{ A }}", "{{ B }}"], {"A": "1", "B": "2"})
        assert result == ["1", "2"]

    def test_non_string_passed_through(self):
        assert render_template(42, {}) == 42
        assert render_template(None, {}) is None


class TestRenderTemplateContext:
    def test_restricted_context_used_when_set(self, mocker):
        mocker.patch.dict(os.environ, {"ALLOWED": "yes", "DENIED": "no"})
        token = render_template_context.set({"ALLOWED": "yes"})
        try:
            assert render_template("{{ ALLOWED }}", render_template_context.get()) == "yes"
            with pytest.raises(jinja2.exceptions.UndefinedError):
                render_template("{{ DENIED }}", render_template_context.get())
        finally:
            render_template_context.reset(token)

    def test_none_context_falls_back_to_os_environ(self, mocker):
        mocker.patch.dict(os.environ, {"FALLBACK_VAR": "fallback_value"})
        assert render_template_context.get() is None
        # When called directly with os.environ, it works
        assert render_template("{{ FALLBACK_VAR }}", dict(os.environ)) == "fallback_value"


class TestTemplateRenderError:
    def test_attributes(self):
        exc = TemplateRenderError(template="{{ x }}", reason="UndefinedError", message="Invalid configuration")
        assert exc.template == "{{ x }}"
        assert exc.reason == "UndefinedError"
        assert exc.message == "Invalid configuration"
