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
define('CATSCAN_VALID_LABELS', ['no_cat', 'my_cat', 'other_cat']);
define('CATSCAN_MAX_UPLOAD_BYTES', 8 * 1024 * 1024); // 8MB, generous for a VGA/SVGA JPEG

/**
 * Activation: make sure an API key exists before anyone tries to configure
 * the server against this site.
 */
function catscan_activate() {
    if (!get_option(CATSCAN_OPTION_API_KEY)) {
        update_option(CATSCAN_OPTION_API_KEY, wp_generate_password(40, false, false));
    }
    catscan_register_post_type();
    flush_rewrite_rules();
}
register_activation_hook(__FILE__, 'catscan_activate');

function catscan_deactivate() {
    flush_rewrite_rules();
}
register_deactivation_hook(__FILE__, 'catscan_deactivate');

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
}
add_action('rest_api_init', 'catscan_register_routes');

function catscan_check_api_key(WP_REST_Request $request) {
    $provided = $request->get_header('x-api-key');
    $expected = get_option(CATSCAN_OPTION_API_KEY);

    if (empty($expected) || empty($provided)) {
        return new WP_Error('catscan_unauthorized', 'Missing API key.', ['status' => 401]);
    }
    if (!hash_equals($expected, $provided)) {
        return new WP_Error('catscan_unauthorized', 'Invalid API key.', ['status' => 401]);
    }
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

    return new WP_REST_Response([
        'success' => true,
        'post_id' => $post_id,
        'image_url' => $attachment_id ? wp_get_attachment_url($attachment_id) : null,
    ], 201);
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

    $api_key = get_option(CATSCAN_OPTION_API_KEY);
    $endpoint = esc_url(rest_url('catscan/v1/detections'));
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
