export interface IntegrationConfig {
  trigger_labels: string[];
  webhook_secret_set: boolean;
  slack_webhook_url_set: boolean;
  slack_webhook_url_masked: string;
  telegram_bot_token_set: boolean;
  telegram_bot_token_masked: string;
  telegram_chat_id: string;
  webhook_notify_url: string;
  webhook_notify_secret_set: boolean;
  webhook_notify_secret_masked: string;
  linear_api_key_set: boolean;
  linear_api_key_masked: string;
  linear_webhook_secret_set: boolean;
  linear_webhook_secret_masked: string;
  linear_repo_map: string;
}

export interface IntegrationConfigUpdate {
  trigger_labels?: string[];
  webhook_secret?: string;
  slack_webhook_url?: string;
  telegram_bot_token?: string;
  telegram_chat_id?: string;
  webhook_notify_url?: string;
  webhook_notify_secret?: string;
  linear_api_key?: string;
  linear_webhook_secret?: string;
  linear_repo_map?: string;
}
