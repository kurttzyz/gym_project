from django import forms
from gym_core.members.forms import MemberProfileForm
from gym_core.memberships.models import MembershipPlan


class SelfProfileForm(MemberProfileForm):
    class Meta(MemberProfileForm.Meta):
        fields = ('full_name','phone','date_of_birth','emergency_contact_name','emergency_contact_phone')


class RenewalForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=MembershipPlan.objects.none())
    starts_on = forms.DateField(widget=forms.DateInput(attrs={'type':'date'}))
    note = forms.CharField(max_length=1000,required=False,widget=forms.Textarea(attrs={'rows':3}))

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['plan'].queryset = MembershipPlan.objects.filter(is_active=True)


class RenewalReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[('approve','Approve quoted term'),('decline','Decline request')])
    note = forms.CharField(max_length=500,widget=forms.Textarea(attrs={'rows':2}))
