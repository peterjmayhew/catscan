<?php
/**
 * Plugin Name: CatScan Detections
 * Description: Receives cat-detection events (image, label, confidence, timestamp) from a CatScan ESP32-CAM server and logs them as posts you can browse, filter, and display.
 * Version: 1.0.0
 * Author: CatScan
 * License: MIT
 * Text Domain: catscan-detections
 */

if (!defined('ABSPATH')) {
    exit; // No direct access.
}

define('CATSCAN_POST_TYPE', 'catscan_detection');
define('CATSCAN_OPTION_API_KEY', 'catscan_api_key');
define('CATSCAN_OPTION_RETENTION_DAYS', 'catscan_retention_days');
define('CATSCAN_OPTION_EMAIL_ALERTS', 'catscan_email_alerts');
define('CATSCAN_VALID_LABELS', ['no_cat', 'my_cat', 'other_cat']);
define('CATSCAN_MAX_UPLOAD_BYTES', 8 * 1024 * 1024); // 8MB, generous for a VGA/SVGA JPEG
define('CATSCAN_CLEANUP_HOOK', 'catscan_daily_cleanup');
define('CATSCAN_CLEANUP_BATCH_SIZE', 200); // per run, so a huge backlog can't time out a request
define('CATSCAN_RATE_LIMIT_MAX', 30);   // requests
define('CATSCAN_RATE_LIMIT_WINDOW', 60); // seconds

/**
 * Activation: make sure an API key exists before anyone tries to configure
 * the server against this site.
 */
function catscan_activate() {
    if (!get_option(CATSCAN_OPTION_API_KEY)) {
        update_option(CATSCAN_OPTION_API_KEY, wp_generate_password(40, false, false));
    }
    if (get_option(CATSCAN_OPTION_RETENTION_DAYS) === false) {
        update_option(CATSCAN_OPTION_RETENTION_DAYS, 90);
    }
    if (get_option(CATSCAN_OPTION_EMAIL_ALERTS) === false) {
        update_option(CATSCAN_OPTION_EMAIL_ALERTS, '1');
    }
    catscan_register_post_type();
    flush_rewrite_rules();

    if (!wp_next_scheduled(CATSCAN_CLEANUP_HOOK)) {
        wp_schedule_event(time(), 'daily', CATSCAN_CLEANUP_HOOK);
    }
}
register_activation_hook(__FILE__, 'catscan_activate');

function catscan_deactivate() {
    flush_rewrite_rules();
    wp_clear_scheduled_hook(CATSCAN_CLEANUP_HOOK);
}
register_deactivation_hook(__FILE__, 'catscan_deactivate');

/**
 * Retention cleanup: without this, detections (and their attached images)
 * accumulate in the media library forever. Runs daily via WP-Cron; set
 * the retention period to 0 on the settings page to disable it.
 */
function catscan_run_cleanup() {
    $retention_days = (int) get_option(CATSCAN_OPTION_RETENTION_DAYS, 90);
    if ($retention_days <= 0) {
        return;
    }

    $old_posts = get_posts([
        'post_type' => CATSCAN_POST_TYPE,
        'post_status' => 'publish',
        'posts_per_page' => CATSCAN_CLEANUP_BATCH_SIZE,
        'fields' => 'ids',
        'date_query' => [[
            'before' => $retention_days . ' days ago',
        ]],
    ]);

    foreach ($old_posts as $post_id) {
        $attachment_id = get_post_thumbnail_id($post_id);
        if ($attachment_id) {
            wp_delete_attachment($attachment_id, true);
        }
        wp_delete_post($post_id, true);
    }
}
add_action(CATSCAN_CLEANUP_HOOK, 'catscan_run_cleanup');

/**
 * Custom post type: one post per logged detection (or per burst-of-frames
 * consensus, if the server is aggregating a burst - see its README).
 */
function catscan_register_post_type() {
    register_post_type(CATSCAN_POST_TYPE, [
        'labels' => [
            'name' => __('Cat Detections', 'catscan-detections'),
            'singular_name' => __('Cat Detection', 'catscan-detections'),
        ],
        'public' => false,
        'show_ui' => true,
        'show_in_menu' => true,
        'menu_icon' => 'dashicons-camera',
        'supports' => ['title', 'thumbnail'],
        'capability_type' => 'post',
        'map_meta_cap' => true,
    ]);
}
add_action('init', 'catscan_register_post_type');

/**
 * REST API: POST /wp-json/catscan/v1/detections
 *
 * Auth: X-Api-Key header must match the key shown on Settings -> CatScan.
 * This is a machine-to-machine endpoint (your own server), not a
 * user-facing one, so a shared secret header is enough - no WP user
 * session/nonce is involved.
 */
function catscan_register_routes() {
    register_rest_route('catscan/v1', '/detections', [
        'methods' => 'POST',
        'callback' => 'catscan_handle_detection_upload',
        'permission_callback' => 'catscan_check_api_key',
    ]);

    // Remote control bridge: the server pushes a status heartbeat here and
    // polls pending-command for anything queued from the Device panel
    // below. Same auth (and rate limit) as /detections.
    register_rest_route('catscan/v1', '/heartbeat', [
        'methods' => 'POST',
        'callback' => 'catscan_handle_heartbeat',
        'permission_callback' => 'catscan_check_api_key',
    ]);
    register_rest_route('catscan/v1', '/pending-command', [
        'methods' => 'GET',
        'callback' => 'catscan_handle_pending_command',
        'permission_callback' => 'catscan_check_api_key',
    ]);
}
add_action('rest_api_init', 'catscan_register_routes');

/**
 * Stores a device status snapshot (uptime, Wi-Fi signal, etc.) for display
 * on Settings -> CatScan -> Device. Only known scalar fields are kept -
 * arbitrary nested structures from the network are never stored as-is.
 */
function catscan_handle_heartbeat(WP_REST_Request $request) {
    $status = $request->get_json_params();
    if (!is_array($status)) {
        $status = [];
    }

    $safe_status = [];
    $known_keys = [
        'reachable', 'uptime_s', 'free_heap', 'wifi_rssi',
        'dark', 'deterrent_enabled', 'seconds_since_last_capture',
    ];
    foreach ($known_keys as $key) {
        if (isset($status[$key]) && is_scalar($status[$key])) {
            $safe_status[$key] = $status[$key];
        }
    }

    // autoload=false: this changes every ~20 seconds and has no business
    // being loaded on every single page request on the site.
    update_option('catscan_device_status', $safe_status, false);
    update_option('catscan_last_heartbeat', time(), false);

    return new WP_REST_Response(['success' => true], 200);
}

/**
 * Returns (and clears) a command queued from the Device panel. The GET
 * itself is the dequeue - there's no separate "acknowledge" step, so a
 * command is considered delivered the moment the server fetches it, even
 * if forwarding it to the ESP32 then fails (the server logs that
 * separately; re-queuing on failure isn't implemented, so re-click the
 * button if a command doesn't seem to have taken effect).
 */
function catscan_handle_pending_command(WP_REST_Request $request) {
    $command = get_option('catscan_pending_command', '');
    if ($command) {
        delete_option('catscan_pending_command');
    }
    return new WP_REST_Response(['command' => $command ?: null], 200);
}

function catscan_check_api_key(WP_REST_Request $request) {
    $provided = $request->get_header('x-api-key');
    $expected = get_option(CATSCAN_OPTION_API_KEY);

    if (empty($expected) || empty($provided)) {
        return new WP_Error('catscan_unauthorized', 'Missing API key.', ['status' => 401]);
    }
    if (!hash_equals($expected, $provided)) {
        return new WP_Error('catscan_unauthorized', 'Invalid API key.', ['status' => 401]);
    }
    if (!catscan_check_rate_limit()) {
        return new WP_Error('catscan_rate_limited', 'Too many requests.', ['status' => 429]);
    }
    return true;
}

/**
 * Basic fixed-window rate limit (abuse protection if the API key ever
 * leaks) via transients - no extra tables, works on any host. A single
 * ESP32-CAM sends nowhere near this volume in normal use.
 */
function catscan_check_rate_limit() {
    $window_start = get_transient('catscan_rl_window_start');

    if ($window_start === false) {
        set_transient('catscan_rl_window_start', time(), CATSCAN_RATE_LIMIT_WINDOW);
        set_transient('catscan_rl_count', 1, CATSCAN_RATE_LIMIT_WINDOW);
        return true;
    }

    $count = (int) get_transient('catscan_rl_count');
    if ($count >= CATSCAN_RATE_LIMIT_MAX) {
        return false;
    }

    // Keep the count transient's expiry in sync with the window's actual
    // remaining time, rather than resetting it to the full window on every
    // request (which would let a steady trickle of requests never expire).
    $remaining = max(1, CATSCAN_RATE_LIMIT_WINDOW - (time() - (int) $window_start));
    set_transient('catscan_rl_count', $count + 1, $remaining);
    return true;
}

function catscan_handle_detection_upload(WP_REST_Request $request) {
    $label = sanitize_text_field($request->get_param('label'));
    if (!in_array($label, CATSCAN_VALID_LABELS, true)) {
        $label = 'no_cat';
    }

    $confidence = (float) $request->get_param('confidence');
    $confidence = max(0.0, min(1.0, $confidence));
    $cat_detected = filter_var($request->get_param('cat_detected'), FILTER_VALIDATE_BOOLEAN);
    $low_light = filter_var($request->get_param('low_light'), FILTER_VALIDATE_BOOLEAN);
    $mode = sanitize_text_field($request->get_param('mode'));
    $frame_count = (int) $request->get_param('frame_count');
    $reasoning = sanitize_textarea_field($request->get_param('reasoning'));

    $attachment_id = catscan_handle_image_upload($request);
    if (is_wp_error($attachment_id)) {
        return $attachment_id;
    }

    $post_id = wp_insert_post([
        'post_type' => CATSCAN_POST_TYPE,
        'post_status' => 'publish',
        'post_title' => sprintf(
            '%s - %s',
            catscan_label_display_name($label),
            current_time('Y-m-d H:i:s')
        ),
    ], true);

    if (is_wp_error($post_id)) {
        return $post_id;
    }

    update_post_meta($post_id, '_catscan_label', $label);
    update_post_meta($post_id, '_catscan_confidence', $confidence);
    update_post_meta($post_id, '_catscan_cat_detected', $cat_detected ? '1' : '0');
    update_post_meta($post_id, '_catscan_low_light', $low_light ? '1' : '0');
    update_post_meta($post_id, '_catscan_mode', $mode);
    update_post_meta($post_id, '_catscan_frame_count', $frame_count);
    if ($reasoning !== '') {
        update_post_meta($post_id, '_catscan_reasoning', $reasoning);
    }

    if ($attachment_id) {
        set_post_thumbnail($post_id, $attachment_id);
    }

    if ($label === 'other_cat' && get_option(CATSCAN_OPTION_EMAIL_ALERTS, '1')) {
        catscan_send_alert_email($post_id, $attachment_id);
    }

    return new WP_REST_Response([
        'success' => true,
        'post_id' => $post_id,
        'image_url' => $attachment_id ? wp_get_attachment_url($attachment_id) : null,
    ], 201);
}

function catscan_send_alert_email($post_id, $attachment_id) {
    $subject = sprintf('[%s] Other cat detected', get_bloginfo('name'));
    $edit_url = admin_url('post.php?post=' . absint($post_id) . '&action=edit');

    $body = "CatScan just logged a visit from a cat that isn't yours.\n\n";
    if ($attachment_id) {
        $body .= 'Photo: ' . wp_get_attachment_url($attachment_id) . "\n\n";
    }
    $body .= 'View in admin: ' . $edit_url . "\n";

    wp_mail(get_option('admin_email'), $subject, $body);
}

function catscan_label_display_name($label) {
    switch ($label) {
        case 'my_cat':
            return 'My cat';
        case 'other_cat':
            return 'Other cat';
        default:
            return 'No cat';
    }
}

/**
 * Validates and stores the uploaded JPEG via WP's own media pipeline
 * (wp_handle_upload + wp_insert_attachment), which handles safe file
 * naming, mime-type checking, and upload-directory placement for us.
 */
function catscan_handle_image_upload(WP_REST_Request $request) {
    $files = $request->get_file_params();
    if (empty($files['image']) || empty($files['image']['tmp_name'])) {
        return null; // Image is optional - a detection can still be logged without one.
    }

    $file = $files['image'];

    if ($file['size'] > CATSCAN_MAX_UPLOAD_BYTES) {
        return new WP_Error('catscan_file_too_large', 'Image exceeds the size limit.', ['status' => 413]);
    }

    $filetype = wp_check_filetype_and_ext($file['tmp_name'], $file['name']);
    if (empty($filetype['ext']) || !in_array($filetype['ext'], ['jpg', 'jpeg'], true)) {
        return new WP_Error('catscan_invalid_file_type', 'Only JPEG images are accepted.', ['status' => 415]);
    }

    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';

    $file['name'] = 'catscan-' . gmdate('Ymd-His') . '-' . wp_generate_password(6, false) . '.jpg';

    $overrides = ['test_form' => false, 'mimes' => ['jpg|jpeg' => 'image/jpeg']];
    $sideloaded = wp_handle_sideload($file, $overrides);

    if (isset($sideloaded['error'])) {
        return new WP_Error('catscan_upload_failed', $sideloaded['error'], ['status' => 500]);
    }

    $attachment_id = wp_insert_attachment([
        'post_mime_type' => 'image/jpeg',
        'post_title' => sanitize_file_name($file['name']),
        'post_status' => 'inherit',
    ], $sideloaded['file']);

    if (is_wp_error($attachment_id)) {
        return $attachment_id;
    }

    $attachment_data = wp_generate_attachment_metadata($attachment_id, $sideloaded['file']);
    wp_update_attachment_metadata($attachment_id, $attachment_data);

    return $attachment_id;
}

/**
 * Admin list table: show the useful fields at a glance instead of just
 * post titles.
 */
function catscan_admin_columns($columns) {
    // Insert our columns right after the checkbox/title columns, keeping
    // 'title' itself intact - it carries the Edit/Trash/View row actions.
    $new_columns = [];
    foreach ($columns as $key => $value) {
        $new_columns[$key] = $value;
        if ($key === 'title') {
            $new_columns['catscan_thumb'] = '';
            $new_columns['catscan_label'] = __('Label', 'catscan-detections');
            $new_columns['catscan_confidence'] = __('Confidence', 'catscan-detections');
            $new_columns['catscan_mode'] = __('Mode', 'catscan-detections');
        }
    }
    return $new_columns;
}
add_filter('manage_' . CATSCAN_POST_TYPE . '_posts_columns', 'catscan_admin_columns');

function catscan_admin_column_content($column, $post_id) {
    switch ($column) {
        case 'catscan_thumb':
            echo get_the_post_thumbnail($post_id, [60, 60]);
            break;
        case 'catscan_label':
            $label = get_post_meta($post_id, '_catscan_label', true);
            $colors = ['my_cat' => '#2271b1', 'other_cat' => '#d63638', 'no_cat' => '#787c82'];
            $color = $colors[$label] ?? '#787c82';
            printf(
                '<span style="color:%s;font-weight:600;">%s</span>',
                esc_attr($color),
                esc_html(catscan_label_display_name($label))
            );
            break;
        case 'catscan_confidence':
            $confidence = (float) get_post_meta($post_id, '_catscan_confidence', true);
            echo esc_html(round($confidence * 100) . '%');
            break;
        case 'catscan_mode':
            echo esc_html(get_post_meta($post_id, '_catscan_mode', true));
            break;
    }
}
add_action('manage_' . CATSCAN_POST_TYPE . '_posts_custom_column', 'catscan_admin_column_content', 10, 2);

/**
 * Label filter dropdown on the admin list screen, so "just the other
 * cat's visits" is a click away instead of scrolling through everything.
 */
function catscan_label_filter_dropdown($post_type) {
    if ($post_type !== CATSCAN_POST_TYPE) {
        return;
    }
    $selected = isset($_GET['catscan_label_filter']) ? sanitize_text_field(wp_unslash($_GET['catscan_label_filter'])) : '';
    echo '<select name="catscan_label_filter">';
    echo '<option value="">' . esc_html__('All labels', 'catscan-detections') . '</option>';
    foreach (CATSCAN_VALID_LABELS as $label) {
        printf(
            '<option value="%s"%s>%s</option>',
            esc_attr($label),
            selected($selected, $label, false),
            esc_html(catscan_label_display_name($label))
        );
    }
    echo '</select>';
}
add_action('restrict_manage_posts', 'catscan_label_filter_dropdown');

function catscan_filter_by_label($query) {
    if (!is_admin() || !$query->is_main_query() || $query->get('post_type') !== CATSCAN_POST_TYPE) {
        return;
    }
    if (empty($_GET['catscan_label_filter'])) {
        return;
    }
    $label = sanitize_text_field(wp_unslash($_GET['catscan_label_filter']));
    if (!in_array($label, CATSCAN_VALID_LABELS, true)) {
        return;
    }
    $meta_query = (array) $query->get('meta_query');
    $meta_query[] = ['key' => '_catscan_label', 'value' => $label];
    $query->set('meta_query', $meta_query);
}
add_action('pre_get_posts', 'catscan_filter_by_label');

/**
 * Settings page: shows the API key and endpoint URL the server needs, and
 * lets the admin regenerate the key if it's ever compromised.
 */
function catscan_register_settings_page() {
    add_options_page(
        'CatScan',
        'CatScan',
        'manage_options',
        'catscan-detections',
        'catscan_render_settings_page'
    );
}
add_action('admin_menu', 'catscan_register_settings_page');

function catscan_render_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }

    if (
        isset($_POST['catscan_regenerate_key'])
        && check_admin_referer('catscan_regenerate_key_action', 'catscan_nonce')
    ) {
        update_option(CATSCAN_OPTION_API_KEY, wp_generate_password(40, false, false));
        echo '<div class="notice notice-success"><p>' . esc_html__('API key regenerated.', 'catscan-detections') . '</p></div>';
    }

    if (
        isset($_POST['catscan_save_settings'])
        && check_admin_referer('catscan_save_settings_action', 'catscan_settings_nonce')
    ) {
        $retention_days = isset($_POST['catscan_retention_days']) ? max(0, (int) $_POST['catscan_retention_days']) : 90;
        update_option(CATSCAN_OPTION_RETENTION_DAYS, $retention_days);
        update_option(CATSCAN_OPTION_EMAIL_ALERTS, isset($_POST['catscan_email_alerts']) ? '1' : '');
        echo '<div class="notice notice-success"><p>' . esc_html__('Settings saved.', 'catscan-detections') . '</p></div>';
    }

    if (
        isset($_POST['catscan_queue_command'])
        && check_admin_referer('catscan_queue_command_action', 'catscan_command_nonce')
    ) {
        $allowed_commands = ['reboot', 'capture', 'deter_test', 'deterrent_on', 'deterrent_off'];
        $command = sanitize_text_field(wp_unslash($_POST['catscan_queue_command']));
        if (in_array($command, $allowed_commands, true)) {
            update_option('catscan_pending_command', $command, false);
            echo '<div class="notice notice-success"><p>' . esc_html__('Command queued - the server picks it up within about 20 seconds.', 'catscan-detections') . '</p></div>';
        }
    }

    $api_key = get_option(CATSCAN_OPTION_API_KEY);
    $endpoint = esc_url(rest_url('catscan/v1/detections'));
    $retention_days = (int) get_option(CATSCAN_OPTION_RETENTION_DAYS, 90);
    $email_alerts = (bool) get_option(CATSCAN_OPTION_EMAIL_ALERTS, '1');
    ?>
    <div class="wrap">
        <h1>CatScan</h1>
        <p><?php esc_html_e('Configure your CatScan server (server/wordpress_client.py) with these values:', 'catscan-detections'); ?></p>
        <table class="form-table">
            <tr>
                <th scope="row"><label for="catscan-url">WORDPRESS_URL</label></th>
                <td><input id="catscan-url" type="text" readonly class="regular-text" value="<?php echo esc_url(site_url()); ?>" onclick="this.select();"></td>
            </tr>
            <tr>
                <th scope="row"><label for="catscan-key">WORDPRESS_API_KEY</label></th>
                <td><input id="catscan-key" type="text" readonly class="regular-text" value="<?php echo esc_attr($api_key); ?>" onclick="this.select();"></td>
            </tr>
            <tr>
                <th scope="row">Endpoint</th>
                <td><code><?php echo esc_html($endpoint); ?></code></td>
            </tr>
        </table>
        <form method="post">
            <?php wp_nonce_field('catscan_regenerate_key_action', 'catscan_nonce'); ?>
            <p class="description"><?php esc_html_e('Regenerating invalidates the current key immediately - update your server\'s WORDPRESS_API_KEY afterwards.', 'catscan-detections'); ?></p>
            <?php submit_button(__('Regenerate API key', 'catscan-detections'), 'delete', 'catscan_regenerate_key'); ?>
        </form>
        <hr>
        <h2><?php esc_html_e('Settings', 'catscan-detections'); ?></h2>
        <form method="post">
            <?php wp_nonce_field('catscan_save_settings_action', 'catscan_settings_nonce'); ?>
            <table class="form-table">
                <tr>
                    <th scope="row"><label for="catscan-retention"><?php esc_html_e('Keep detections for', 'catscan-detections'); ?></label></th>
                    <td>
                        <input id="catscan-retention" type="number" min="0" name="catscan_retention_days" value="<?php echo esc_attr($retention_days); ?>" class="small-text">
                        <?php esc_html_e('days (0 = keep forever)', 'catscan-detections'); ?>
                        <p class="description"><?php esc_html_e('Older detections and their photos are deleted automatically once a day.', 'catscan-detections'); ?></p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><?php esc_html_e('Email alerts', 'catscan-detections'); ?></th>
                    <td>
                        <label>
                            <input type="checkbox" name="catscan_email_alerts" value="1" <?php checked($email_alerts); ?>>
                            <?php esc_html_e('Email the site admin when an "other cat" visit is logged', 'catscan-detections'); ?>
                        </label>
                    </td>
                </tr>
            </table>
            <?php submit_button(__('Save settings', 'catscan-detections'), 'primary', 'catscan_save_settings'); ?>
        </form>
        <hr>
        <h2><?php esc_html_e('Device', 'catscan-detections'); ?></h2>
        <?php
        $last_heartbeat = (int) get_option('catscan_last_heartbeat', 0);
        $device_status = get_option('catscan_device_status', []);
        if (!is_array($device_status)) {
            $device_status = [];
        }
        // 90s tolerates a few missed beats at the server's default ~20s
        // heartbeat interval without flapping "offline" on one dropped one.
        $bridge_online = $last_heartbeat > 0 && (time() - $last_heartbeat) < 90;
        $camera_reachable = $bridge_online && isset($device_status['uptime_s']);
        ?>
        <p>
            <?php if ($last_heartbeat === 0) : ?>
                <?php esc_html_e('No status received yet - check WORDPRESS_URL/WORDPRESS_API_KEY on your server.', 'catscan-detections'); ?>
            <?php elseif (!$bridge_online) : ?>
                <strong style="color:#d63638;"><?php esc_html_e('Bridge server offline', 'catscan-detections'); ?></strong>
                &mdash; <?php printf(esc_html__('last checked in %s ago. Is server/app.py still running?', 'catscan-detections'), esc_html(human_time_diff($last_heartbeat, time()))); ?>
            <?php elseif (!$camera_reachable) : ?>
                <strong style="color:#dba617;"><?php esc_html_e('Server online, camera unreachable', 'catscan-detections'); ?></strong>
                &mdash; <?php esc_html_e('check the ESP32 is powered on and ESP32_CONTROL_URL is correct.', 'catscan-detections'); ?>
            <?php else : ?>
                <strong style="color:#2271b1;"><?php esc_html_e('Online', 'catscan-detections'); ?></strong>
                &mdash; <?php printf(esc_html__('last checked in %s ago', 'catscan-detections'), esc_html(human_time_diff($last_heartbeat, time()))); ?>
            <?php endif; ?>
        </p>
        <?php if ($camera_reachable) : ?>
            <table class="form-table">
                <?php if (isset($device_status['uptime_s'])) : ?>
                    <tr><th scope="row"><?php esc_html_e('Uptime', 'catscan-detections'); ?></th><td><?php echo esc_html(human_time_diff(time() - (int) $device_status['uptime_s'], time())); ?></td></tr>
                <?php endif; ?>
                <?php if (isset($device_status['wifi_rssi'])) : ?>
                    <tr><th scope="row"><?php esc_html_e('Wi-Fi signal', 'catscan-detections'); ?></th><td><?php echo esc_html($device_status['wifi_rssi']); ?> dBm</td></tr>
                <?php endif; ?>
                <?php if (isset($device_status['dark'])) : ?>
                    <tr><th scope="row"><?php esc_html_e('Ambient light', 'catscan-detections'); ?></th><td><?php echo $device_status['dark'] ? esc_html__('Dark', 'catscan-detections') : esc_html__('Light', 'catscan-detections'); ?></td></tr>
                <?php endif; ?>
                <?php if (isset($device_status['deterrent_enabled'])) : ?>
                    <tr><th scope="row"><?php esc_html_e('Auto-deterrent', 'catscan-detections'); ?></th><td><?php echo $device_status['deterrent_enabled'] ? esc_html__('Enabled', 'catscan-detections') : esc_html__('Disabled', 'catscan-detections'); ?></td></tr>
                <?php endif; ?>
                <?php if (isset($device_status['seconds_since_last_capture']) && (int) $device_status['seconds_since_last_capture'] >= 0) : ?>
                    <tr><th scope="row"><?php esc_html_e('Last capture', 'catscan-detections'); ?></th><td><?php printf(esc_html__('%s ago', 'catscan-detections'), esc_html(human_time_diff(time() - (int) $device_status['seconds_since_last_capture'], time()))); ?></td></tr>
                <?php endif; ?>
            </table>
        <?php endif; ?>
        <form method="post">
            <?php wp_nonce_field('catscan_queue_command_action', 'catscan_command_nonce'); ?>
            <button type="submit" name="catscan_queue_command" value="capture" class="button"><?php esc_html_e('Capture now', 'catscan-detections'); ?></button>
            <button type="submit" name="catscan_queue_command" value="deter_test" class="button"><?php esc_html_e('Test deterrent', 'catscan-detections'); ?></button>
            <button type="submit" name="catscan_queue_command" value="deterrent_on" class="button"><?php esc_html_e('Enable auto-deterrent', 'catscan-detections'); ?></button>
            <button type="submit" name="catscan_queue_command" value="deterrent_off" class="button"><?php esc_html_e('Disable auto-deterrent', 'catscan-detections'); ?></button>
            <button type="submit" name="catscan_queue_command" value="reboot" class="button button-secondary" onclick="return confirm('<?php echo esc_js(__('Reboot the device now?', 'catscan-detections')); ?>');"><?php esc_html_e('Reboot device', 'catscan-detections'); ?></button>
            <p class="description"><?php esc_html_e('Commands are queued here and picked up by the server on its next status check (~20 seconds), then forwarded to the ESP32 - not instant.', 'catscan-detections'); ?></p>
        </form>
        <hr>
        <p><?php esc_html_e('Display recent detections anywhere with the shortcode:', 'catscan-detections'); ?> <code>[catscan_recent limit="12" label="all"]</code></p>
    </div>
    <?php
}

/**
 * Shortcode: [catscan_recent limit="12" label="all|my_cat|other_cat|no_cat"]
 * Renders a simple responsive grid of recent detections.
 */
function catscan_recent_shortcode($atts) {
    $atts = shortcode_atts([
        'limit' => 12,
        'label' => 'all',
    ], $atts, 'catscan_recent');

    $args = [
        'post_type' => CATSCAN_POST_TYPE,
        'posts_per_page' => max(1, min(100, (int) $atts['limit'])),
        'post_status' => 'publish',
    ];

    if (in_array($atts['label'], CATSCAN_VALID_LABELS, true)) {
        $args['meta_query'] = [[
            'key' => '_catscan_label',
            'value' => $atts['label'],
        ]];
    }

    $query = new WP_Query($args);
    if (!$query->have_posts()) {
        return '<p>' . esc_html__('No detections yet.', 'catscan-detections') . '</p>';
    }

    ob_start();
    echo '<div class="catscan-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;">';
    while ($query->have_posts()) {
        $query->the_post();
        $post_id = get_the_ID();
        $label = get_post_meta($post_id, '_catscan_label', true);
        $confidence = (float) get_post_meta($post_id, '_catscan_confidence', true);
        echo '<div style="border:1px solid #ddd;border-radius:6px;overflow:hidden;">';
        echo get_the_post_thumbnail($post_id, 'medium', ['style' => 'width:100%;display:block;']);
        printf(
            '<div style="padding:6px 8px;font-size:13px;">%s &middot; %s%% &middot; %s</div>',
            esc_html(catscan_label_display_name($label)),
            esc_html(round($confidence * 100)),
            esc_html(get_the_date())
        );
        echo '</div>';
    }
    echo '</div>';
    wp_reset_postdata();

    return ob_get_clean();
}
add_shortcode('catscan_recent', 'catscan_recent_shortcode');
