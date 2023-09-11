import json

from django import forms
from django.core.signing import BadSignature, Signer
from django.utils.translation import gettext_lazy as _

from allauth.account.adapter import get_adapter as get_account_adapter
from allauth.account.models import EmailAddress
from allauth.core import context, ratelimit
from allauth.mfa import totp
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.utils import post_authentication
from allauth.mfa.webauthn import (
    begin_authentication,
    begin_registration,
    complete_authentication,
    complete_registration,
    get_credentials,
    parse_authentication_credential,
    parse_registration_credential,
)


class AuthenticateForm(forms.Form):
    code = forms.CharField(
        label=_("Code"),
        widget=forms.TextInput(
            attrs={"placeholder": _("Code"), "autocomplete": "one-time-code"},
        ),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        super().__init__(*args, **kwargs)

    def clean_code(self):
        key = f"mfa-auth-user-{str(self.user.pk)}"
        if not ratelimit.consume(
            context.request,
            action="login_failed",
            key=key,
        ):
            raise forms.ValidationError(
                get_account_adapter().error_messages["too_many_login_attempts"]
            )

        code = self.cleaned_data["code"]
        for auth in Authenticator.objects.filter(user=self.user).exclude(
            type=Authenticator.Type.WEBAUTHN
        ):
            if auth.wrap().validate_code(code):
                self.authenticator = auth
                ratelimit.clear(context.request, action="login_failed", key=key)
                return code
        raise forms.ValidationError(get_adapter().error_messages["incorrect_code"])

    def save(self):
        post_authentication(context.request, self.authenticator)


class AuthenticateWebAuthnForm(forms.Form):
    signed_state = forms.CharField(required=False, widget=forms.HiddenInput)
    credential = forms.CharField(required=True, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        initial = kwargs.setdefault("initial", {})
        self.user = kwargs.pop("user")
        self.authentication_data, state = begin_authentication(self.user)
        initial["signed_state"] = Signer().sign(json.dumps(state))
        super().__init__(*args, **kwargs)

    def clean_signed_state(self):
        signed_state = self.cleaned_data["signed_state"]
        try:
            return json.loads(Signer().unsign(signed_state))
        except BadSignature:
            raise forms.ValidationError("Tampered form.")

    def clean_credential(self):
        credential = self.cleaned_data["credential"]
        return parse_authentication_credential(json.loads(credential))

    def clean(self):
        cleaned_data = super().clean()
        state = cleaned_data.get("signed_state")
        if all([cleaned_data["credential"], state]):
            cleaned_data["authenticator_data"] = complete_authentication(
                state, get_credentials(self.user), cleaned_data["credential"]
            )
        return cleaned_data


class ActivateTOTPForm(forms.Form):
    code = forms.CharField(
        label=_("Authenticator code"),
        widget=forms.TextInput(
            attrs={"placeholder": _("Code"), "autocomplete": "one-time-code"},
        ),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        self.email_verified = not EmailAddress.objects.filter(
            user=self.user, verified=False
        ).exists()
        super().__init__(*args, **kwargs)
        self.secret = totp.get_totp_secret(regenerate=not self.is_bound)

    def clean_code(self):
        try:
            code = self.cleaned_data["code"]
            if not self.email_verified:
                raise forms.ValidationError(
                    get_adapter().error_messages["unverified_email"]
                )
            if not totp.validate_totp_code(self.secret, code):
                raise forms.ValidationError(
                    get_adapter().error_messages["incorrect_code"]
                )
            return code
        except forms.ValidationError as e:
            self.secret = totp.get_totp_secret(regenerate=True)
            raise e


class DeactivateTOTPForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.authenticator = kwargs.pop("authenticator")
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        adapter = get_adapter()
        if not adapter.can_delete_authenticator(self.authenticator):
            raise forms.ValidationError(
                adapter.error_messages["cannot_delete_authenticator"]
            )
        return cleaned_data


class AddWebAuthnForm(forms.Form):
    name = forms.CharField(required=False)
    passwordless = forms.BooleanField(
        label=_("Passwordless"),
        required=False,
        help_text=_(
            "Enabling passwordless operation allows you to sign in using just this key/device, but imposes additional requirements such as biometrics or PIN protection."
        ),
    )
    credential = forms.CharField(required=True, widget=forms.HiddenInput)
    signed_state = forms.CharField(required=True, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        initial = kwargs.setdefault("initial", {})
        self.user = kwargs.pop("user")
        self.registration_data, state = begin_registration()
        initial["signed_state"] = Signer().sign(json.dumps(state))
        super().__init__(*args, **kwargs)

    def clean_signed_state(self):
        signed_state = self.cleaned_data["signed_state"]
        try:
            return json.loads(Signer().unsign(signed_state))
        except BadSignature:
            raise forms.ValidationError("Tampered form.")

    def clean_credential(self):
        credential = self.cleaned_data["credential"]
        return parse_registration_credential(json.loads(credential))

    def clean(self):
        cleaned_data = super().clean()
        state = cleaned_data.get("signed_state")
        credential = cleaned_data.get("credential")
        passwordless = cleaned_data.get("passwordless")
        if credential:
            if (
                passwordless
                and not credential["attestation_object"].auth_data.is_user_verified()
            ):
                self.add_error(
                    None, _("This key does not support passwordless operation.")
                )
        if all([credential, state]):
            cleaned_data["authenticator_data"] = complete_registration(
                state, credential
            )
        return cleaned_data
