// Copyright (c) 2026, Hypedrive
// License: MIT

frappe.ui.form.on("Chatwoot Settings", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Test Connection"), () => {
			frm.call({
				doc: frm.doc,
				method: "test_connection",
				freeze: true,
				freeze_message: __("Testing connection to Chatwoot..."),
				callback: (r) => {
					if (!r.exc && r.message) {
						const agent = r.message.name || r.message.email || __("Unknown agent");
						frappe.show_alert({
							message: __("Connected to Chatwoot as {0}", [agent]),
							indicator: "green",
						});
					}
				},
			});
		});

		frm.add_custom_button(__("Clear Cache"), () => {
			frappe.confirm(
				__("Clear all cached Chatwoot API responses? The next read from any open conversation panel will hit Chatwoot directly."),
				() => {
					frappe.call({
						method: "frappe_chatwoot.api.chatwoot.clear_chatwoot_cache",
						freeze: true,
						freeze_message: __("Clearing cache..."),
						callback: (r) => {
							if (!r.exc) {
								frappe.show_alert({
									message: __("Chatwoot cache cleared"),
									indicator: "green",
								});
							}
						},
					});
				}
			);
		});
	},
});
