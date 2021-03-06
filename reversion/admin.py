from __future__ import unicode_literals
from contextlib import contextmanager
from django.db import models, transaction, connection
from django.conf.urls import url
from django.contrib import admin, messages
from django.contrib.admin import options
from django.contrib.admin.utils import unquote, quote
try:
    from django.contrib.contenttypes.admin import GenericInlineModelAdmin
    from django.contrib.contenttypes.fields import GenericRelation
except ImportError:  # Django < 1.9  pragma: no cover
    from django.contrib.contenttypes.generic import GenericInlineModelAdmin, GenericRelation
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied, ImproperlyConfigured
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.text import capfirst
from django.utils.translation import ugettext as _
from django.utils.encoding import force_text
from django.utils.formats import localize
from reversion.compat import remote_field, remote_model
from reversion.errors import RevertError
from reversion.models import Version
from reversion.revisions import default_revision_manager


class RollBackRevisionView(Exception):

    pass


class VersionAdmin(admin.ModelAdmin):

    """Abstract admin class for handling version controlled models."""

    object_history_template = "reversion/object_history.html"

    change_list_template = "reversion/change_list.html"

    revision_form_template = None
    """The template to render the revision form."""

    recover_list_template = None
    """The template to render the recover list."""

    recover_form_template = None
    """The template to render the recover form."""

    revision_manager = default_revision_manager
    """The revision manager used to manage revisions."""

    reversion_format = "json"
    """The serialization format to use when registering models."""

    ignore_duplicate_revisions = False
    """Whether to ignore duplicate revision data."""

    history_latest_first = False
    """Display versions with the most recent version first."""

    def reversion_register(self, model, **kwargs):
        """Registers the model with reversion."""
        self.revision_manager.register(model, **kwargs)

    # Revision helpers.

    @property
    def revision_context_manager(self):
        """The revision context manager for this VersionAdmin."""
        return self.revision_manager._revision_context_manager

    @contextmanager
    def create_revision(self, request):
        """
        Marks up a block of code as requiring a revision to be created.

        Set the revision user as the current user, and honours admin reversion configuration.
        """
        with self.revision_context_manager.create_revision():
            self.revision_context_manager.set_user(request.user)
            self.revision_context_manager.set_ignore_duplicates(self.ignore_duplicate_revisions)
            yield

    def _reversion_get_template_list(self, template_name):
        opts = self.model._meta
        return (
            "reversion/%s/%s/%s" % (opts.app_label, opts.object_name.lower(), template_name),
            "reversion/%s/%s" % (opts.app_label, template_name),
            "reversion/%s" % template_name,
        )

    def _reversion_order_version_queryset(self, queryset):
        """Applies the correct ordering to the given version queryset."""
        if self.history_latest_first:
            return queryset.order_by("-pk")
        return queryset.order_by("pk")

    # Messages.

    def log_addition(self, request, object, change_message=None):
        change_message = change_message or _("Initial version.")
        if self.revision_context_manager.is_active():
            self.revision_context_manager.set_comment(change_message)
        try:
            super(VersionAdmin, self).log_addition(request, object, change_message)
        except TypeError:  # Django < 1.9 pragma: no cover
            super(VersionAdmin, self).log_addition(request, object)

    def log_change(self, request, object, message):
        if self.revision_context_manager.is_active():
            self.revision_context_manager.set_comment(message)
        super(VersionAdmin, self).log_change(request, object, message)

    # Auto-registration.

    def _reversion_autoregister(self, model, follow):
        if not self.revision_manager.is_registered(model):
            for parent_cls, field in model._meta.concrete_model._meta.parents.items():
                follow += (field.name,)
                self._reversion_autoregister(parent_cls, ())
            self.reversion_register(model, follow=follow, format=self.reversion_format)

    def _reversion_introspect_inline_admin(self, inline):
        inline_model = None
        follow_field = None
        fk_name = None
        if issubclass(inline, GenericInlineModelAdmin):
            inline_model = inline.model
            ct_field = inline.ct_field
            fk_name = inline.ct_fk_field
            for field in self.model._meta.virtual_fields:
                if (
                    isinstance(field, GenericRelation) and
                    remote_model(field) == inline_model and
                    field.object_id_field_name == fk_name and
                    field.content_type_field_name == ct_field
                ):
                    follow_field = field.name
                    break
        elif issubclass(inline, options.InlineModelAdmin):
            inline_model = inline.model
            fk_name = inline.fk_name
            if not fk_name:
                for field in inline_model._meta.get_fields():
                    if (
                        isinstance(field, (models.ForeignKey, models.OneToOneField)) and
                        issubclass(self.model, remote_model(field))
                    ):
                        fk_name = field.name
                        break
            if fk_name and not remote_field(inline_model._meta.get_field(fk_name)).is_hidden():
                field = inline_model._meta.get_field(fk_name)
                accessor = remote_field(field).get_accessor_name()
                follow_field = accessor
        return inline_model, follow_field, fk_name

    def __init__(self, *args, **kwargs):
        super(VersionAdmin, self).__init__(*args, **kwargs)
        # Check that database transactions are supported.
        if not connection.features.uses_savepoints:
            raise ImproperlyConfigured("Cannot use VersionAdmin with a database that does not support savepoints.")
        # Automatically register models if required.
        if not self.revision_manager.is_registered(self.model):
            inline_fields = ()
            for inline in self.inlines:
                inline_model, follow_field, _ = self._reversion_introspect_inline_admin(inline)
                if inline_model:
                    self._reversion_autoregister(inline_model, ())
                if follow_field:
                    inline_fields += (follow_field,)
            self._reversion_autoregister(self.model, inline_fields)

    def get_urls(self):
        urls = super(VersionAdmin, self).get_urls()
        admin_site = self.admin_site
        opts = self.model._meta
        info = opts.app_label, opts.model_name,
        reversion_urls = [
            url("^recover/$", admin_site.admin_view(self.recoverlist_view), name='%s_%s_recoverlist' % info),
            url("^recover/(\d+)/$", admin_site.admin_view(self.recover_view), name='%s_%s_recover' % info),
            url("^([^/]+)/history/(\d+)/$", admin_site.admin_view(self.revision_view), name='%s_%s_revision' % info),
        ]
        return reversion_urls + urls

    # Views.

    def add_view(self, request, form_url='', extra_context=None):
        with self.create_revision(request):
            return super(VersionAdmin, self).add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        with self.create_revision(request):
            return super(VersionAdmin, self).change_view(request, object_id, form_url, extra_context)

    def revisionform_view(self, request, version, template_name, extra_context=None):
        try:
            with transaction.atomic():
                # Revert the revision.
                version.revision.revert(delete=True)
                # Run the normal changeform view.
                with self.create_revision(request):
                    response = self.changeform_view(request, version.object_id, request.path, extra_context)
                    # Decide on whether the keep the changes.
                    if request.method == "POST" and response.status_code == 302:
                        self.revision_context_manager.set_comment(
                            _("Reverted to previous version, saved on %(datetime)s") % {
                                "datetime": localize(version.revision.date_created),
                            }
                        )
                    else:
                        response.template_name = template_name  # Set the template name to the correct template.
                        response.render()  # Eagerly render the response, so it's using the latest version.
                        raise RollBackRevisionView  # Raise an exception to undo the transaction and the revision.
        except RevertError as ex:
            opts = self.model._meta
            messages.error(request, force_text(ex))
            return redirect("%s:%s_%s_changelist" % (self.admin_site.name, opts.app_label, opts.model_name))
        except RollBackRevisionView:
            pass
        return response

    def recover_view(self, request, version_id, extra_context=None):
        """Displays a form that can recover a deleted model."""
        # The revisionform view will check for change permission (via changeform_view),
        # but we also need to check for add permissions here.
        if not self.has_add_permission(request):
            raise PermissionDenied
        # Render the recover view.
        version = get_object_or_404(Version, pk=version_id)
        context = {
            "title": _("Recover %(name)s") % {"name": version.object_repr},
        }
        context.update(extra_context or {})
        return self.revisionform_view(
            request,
            version,
            self.recover_form_template or self._reversion_get_template_list("recover_form.html"),
            context,
        )

    def revision_view(self, request, object_id, version_id, extra_context=None):
        """Displays the contents of the given revision."""
        object_id = unquote(object_id)  # Underscores in primary key get quoted to "_5F"
        version = get_object_or_404(Version, pk=version_id, object_id=object_id)
        context = {
            "title": _("Revert %(name)s") % {"name": version.object_repr},
        }
        context.update(extra_context or {})
        return self.revisionform_view(
            request,
            version,
            self.revision_form_template or self._reversion_get_template_list("revision_form.html"),
            context,
        )

    def changelist_view(self, request, extra_context=None):
        with self.create_revision(request):
            context = {
                "has_change_permission": self.has_change_permission(request),
            }
            context.update(extra_context or {})
            return super(VersionAdmin, self).changelist_view(request, context)

    def recoverlist_view(self, request, extra_context=None):
        """Displays a deleted model to allow recovery."""
        # Check if user has change and add permissions for model
        if not self.has_change_permission(request) or not self.has_add_permission(request):
            raise PermissionDenied
        model = self.model
        opts = model._meta
        deleted = self._reversion_order_version_queryset(self.revision_manager.get_deleted(self.model))
        # Get the site context.
        try:
            each_context = self.admin_site.each_context(request)
        except TypeError:  # Django <= 1.7 pragma: no cover
            each_context = self.admin_site.each_context()
        # Set the app name.
        request.current_app = self.admin_site.name
        # Get the rest of the context.
        context = dict(
            each_context,
            opts=opts,
            app_label=opts.app_label,
            module_name=capfirst(opts.verbose_name),
            title=_("Recover deleted %(name)s") % {"name": force_text(opts.verbose_name_plural)},
            deleted=deleted,
        )
        context.update(extra_context or {})
        return render(
            request,
            self.recover_list_template or self._reversion_get_template_list("recover_list.html"),
            context,
        )

    def history_view(self, request, object_id, extra_context=None):
        """Renders the history view."""
        # Check if user has change permissions for model
        if not self.has_change_permission(request):
            raise PermissionDenied
        object_id = unquote(object_id)  # Underscores in primary key get quoted to "_5F"
        opts = self.model._meta
        action_list = [
            {
                "revision": version.revision,
                "url": reverse(
                    "%s:%s_%s_revision" % (self.admin_site.name, opts.app_label, opts.model_name),
                    args=(quote(version.object_id), version.id)
                ),
            }
            for version
            in self._reversion_order_version_queryset(self.revision_manager.get_for_object_reference(
                self.model,
                object_id,
            ).select_related("revision__user"))
        ]
        # Compile the context.
        context = {"action_list": action_list}
        context.update(extra_context or {})
        return super(VersionAdmin, self).history_view(request, object_id, context)
