#  Drakkar-Software QuantGuardBot-Interfaces
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import flask

import octobot_commons.authentication as authentication
import tentacles.Services.Interfaces.web_interface.login as login
import tentacles.Services.Interfaces.web_interface.models as models


def register(blueprint):
    @blueprint.route("/community")
    @login.login_required_when_activated
    def community():
        authenticator = authentication.Authenticator.instance()
        logged_in_email = None
        use_preview = not authenticator.can_authenticate()
        try:
            models.wait_for_login_if_processing()
            logged_in_email = authenticator.get_logged_in_email()
        except (authentication.AuthenticationRequired, authentication.UnavailableError):
            pass
        except Exception as e:
            flask.flash(f"Error when contacting the community server: {e}", "error")
        if logged_in_email is None and not use_preview:
            return flask.redirect('community_login')
        strategies = models.get_cloud_strategies(authenticator)
        return flask.render_template(
            'community.html',
            current_logged_in_email=logged_in_email,
            role=authenticator.user_account.supports.support_role,
            is_donor=bool(authenticator.user_account.supports.is_donor()),
            strategies=strategies,
            current_bots_stats=models.get_current_octobots_stats(),
            all_user_bots=models.get_all_user_bots(),
            selected_user_bot=models.get_selected_user_bot(),
            can_logout=models.can_logout(),
            can_select_bot=models.can_select_bot(),
        )


    @blueprint.route("/community_metrics")
    @login.login_required_when_activated
    def community_metrics():
        return flask.redirect("/")
        can_get_metrics = models.can_get_community_metrics()
        display_metrics = models.get_community_metrics_to_display() if can_get_metrics else None
        return flask.render_template('community_metrics.html',
                                     can_get_metrics=can_get_metrics,
                                     community_metrics=display_metrics
                                     )
