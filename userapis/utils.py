from django.conf import settings
from django.core.mail import send_mail
from django.template import loader
from ecpmpapp.alt_messages import plain_messages_for_send_feedback_notification_email
from models.models import Preference
def send_feedback_notification_email(admin, feedback):
    try:
        context = {
            "user_name": feedback.user.full_name if feedback.user else feedback.freelancer.full_name,
            "solution_name": feedback.solution_name,
            "feedback_type": feedback.feedback_type,
            "feedback_link": f"{settings.FRONTEND_URL_BACK_OFFICE}/feedbacks",
        }
        html_body = loader.render_to_string('notify_feedback_admin.html', context)
        plain_messages = plain_messages_for_send_feedback_notification_email(
            user_name=context["user_name"],
            solution_name=context["solution_name"],
            feedback_type=context["feedback_type"],
            feedback_link=context["feedback_link"],
        )
        email_check = get_user_email_preferences(admin.id)
        if email_check:
            send_mail(
                subject="Feedback on sharemundo links® received",
                message=plain_messages,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin.email],
            )
    except Exception as e:
        print(f"Error sending feedback notification email: {e}")


def get_user_email_preferences(user_id):
    try:
        preference = Preference.objects.get(user_id=user_id)
        return preference.email_check
    except Preference.DoesNotExist:
            return None
