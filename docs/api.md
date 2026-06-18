# Web UI API リファレンス

`production_tool/config_server.py` が提供するHTTP APIのまとめです。Androidクライアントなど外部からこの機器を操作する際の参照用です。実装（`config_server.py`）が正であり、本ドキュメントはそのスナップショットなので、挙動が食い違う場合はソースを確認してください。

## 基本情報

- **ベースURL**: `http://<Cube-J1のIPアドレス>:<web_port>/`（`web_port`のデフォルトは`8080`。`80`は本体のnginxが使用しているため設定不可）
- **認証**: HTTP Basic認証。全エンドポイント共通（`config.json`の`web_user`/`web_pass`）。未認証時は`401`＋`WWW-Authenticate: Basic realm="Cube J1 MQTT Config"`。
- **文字コード**: レスポンスは全て`UTF-8`。
- **重要な注意**: `POST`系エンドポイント（`/save`, `/reboot`, `/ota/upload`, `/ota/rollback`, `/config/import`）は**レスポンスボディがHTML**（Web UIのページ全体を再レンダリングしたもの）です。JSONは返りません。成功/失敗の判定は**HTTPステータスコード**（`200`=成功、`400`/`500`=失敗）で行い、詳細な状態を知りたい場合は直後に`GET /status.json`や`GET /ota_status.json`を呼んでください。

---

## GET エンドポイント（JSON / テキスト）

### `GET /status.json`
MQTTブリッジ（`mqtt_bridge.py`）の現在の状態。`mqtt_bridge.py`が`/data/local/mqtt_status.json`に書き込んだものをそのまま返します。

主なフィールド:

| フィールド | 型 | 説明 |
|---|---|---|
| `configuration_required` | bool | `br_id`/`br_pwd`/`mqtt_host`が未設定で待機中か |
| `missing_config` | string[] | 不足している設定キー |
| `mqtt_connected` | bool | MQTTブローカーへの接続状態 |
| `mqtt_host` / `mqtt_port` | string/number | 接続先MQTTブローカー |
| `wisun_connected` | bool | Wi-SUN（スマートメーター）への接続状態 |
| `meter_ipv6` | string | スマートメーターのIPv6アドレス |
| `device_id` | string | デバイスID |
| `serial_port` | string | シリアルポート |
| `poll_interval` | number | ポーリング間隔（秒） |
| `bridge_started_at` | string | ブリッジ起動時刻（JST） |
| `last_measurement_at` | string\|null | 最終計測成功時刻（JST） |
| `last_values` | object | 直近の計測値（`power_w`, `energy_forward_kwh`, `energy_reverse_kwh`, `current_r_a`, `current_t_a`, `one_minute_energy_forward_kwh`, `one_minute_energy_reverse_kwh`, `fixed_time_energy_forward_kwh`, `fixed_time_energy_reverse_kwh`, `operation_status`, `fault_status`, `meter_date`, `meter_time`のうち取得できたもの） |
| `gettable_epcs` / `polling_epcs` | string[] | メーターが対応するEPC／実際にポーリングしているEPC（`"0xE7"`形式） |
| `last_error` | string | 直近のエラーメッセージ（正常時は空文字） |
| `updated_at` | string | このJSONの最終更新時刻（JST） |

### `GET /ota_status.json`
直近のOTA適用・ロールバック結果。

| フィールド | 型 | 説明 |
|---|---|---|
| `state` | string | `idle` / `applying` / `success` / `failed` / `rolled_back` |
| `message` | string | 状態の説明文 |
| `updated_at` | string | 更新時刻（JST） |
| `version` | string | 適用/ロールバック後のバージョン文字列 |

### `GET /config.json`
現在の`config.json`の内容をそのまま返します（`br_pwd`/`mqtt_pass`/`web_pass`等も平文で含まれるので扱いに注意）。フィールドは下記「`config.json`スキーマ」参照。

### `GET /mqtt_bridge.log`
MQTTブリッジの動作ログ（テキスト、`text/plain`）。末尾最大256KB。ログが無い場合は`No log yet`を返す。

### `GET /serial.log`
`/dev/ttyS1`との生シリアル通信ログ（テキスト、`text/plain`）。末尾最大256KB。

### `GET /` または `GET /index.html`
Web UI本体（HTML）。Androidクライアントからは通常不要。

---

## POST エンドポイント

### `POST /save`
設定を保存する。Content-Type: `application/x-www-form-urlencoded`。

送信パラメータ（全て文字列、`FIELDS`定義順）:

| キー | 説明 | 検証ルール |
|---|---|---|
| `br_id` | Bルート認証ID | - |
| `br_pwd` | Bルートパスワード | - |
| `mqtt_host` | MQTTブローカーのホスト/IP | `.local`等のmDNS名は本体側で解決できないためIP推奨 |
| `mqtt_port` | MQTTポート | 数値、1〜65535 |
| `mqtt_user` / `mqtt_pass` | MQTT認証情報 | - |
| `device_id` | デバイスID | avahiのmDNSホスト名（`<device_id>.local`）にも反映される |
| `serial_port` | シリアルポート | 通常`/dev/ttyS1`固定 |
| `poll_interval` | ポーリング間隔（秒） | 数値、1以上 |
| `web_port` | Web UIのポート | 数値、1〜65535。**80は不可**（本体nginxと衝突） |
| `web_user` / `web_pass` | Web UI Basic認証情報 | `web_user`は空不可 |
| `restart_bridge` | `"1"`を送るとMQTTブリッジを再起動 | 任意 |

挙動:
- 全フィールドを毎回送ること（未送信のフィールドは空文字として扱われ上書きされる）
- 検証エラー時は`400`
- 保存成功時は`200`。`device_id`が変わっていればavahiホスト名を自動同期（変更時のみavahi再起動）
- `web_port`変更は**ブリッジ自身の再起動が必要**（`POST /reboot`または`adb`での`cubej_web_ui`再起動）

### `POST /reboot`
本体を再起動する（レスポンス送信後1秒待ってバックグラウンドで`reboot`実行）。パラメータ不要。`200`固定。

### `POST /ota/upload`
OTAパッケージ（zip）を適用する。Content-Type: `multipart/form-data`。

| フィールド名 | 説明 |
|---|---|
| `package` | OTAパッケージのzipファイル（最大2MB） |

挙動:
- `manifest.json`・SHA-256・対象パス・互換性を検証してから適用
- 受理されると`200`＋"OTA update accepted"のHTML。実際の適用結果は非同期（数秒後に`GET /ota_status.json`で確認）
- 不正なパッケージは`400`

### `POST /ota/rollback`
直前の`.bak`バックアップに戻す。パラメータ不要。

- バックアップが存在しない場合は`400`（Web UI上もボタンが無効化される条件と同じ）
- 受理されると`200`。適用は非同期

### `POST /config/import`
`config.json`をアップロードして置き換える。Content-Type: `multipart/form-data`。

| フィールド名 | 説明 |
|---|---|
| `config` | JSONファイル（最大64KB） |

挙動: バリデーション通過後に保存・avahi同期・ブリッジ再起動まで実行。

---

## `config.json` スキーマ

```json
{
    "br_id": "",
    "br_pwd": "",
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "device_id": "cubej1",
    "serial_port": "/dev/ttyS1",
    "poll_interval": 60,
    "web_port": 8080,
    "web_user": "admin",
    "web_pass": "cubej1"
}
```

`br_id` / `br_pwd` / `mqtt_host` のいずれかが空の間、ブリッジは`status.json`の`configuration_required=true`のまま待機する。

---

## Androidクライアント実装上の補足

- 既存のHTML版Web UIと全く同じプロトコルなので、別途専用APIを作る必要はない
- 認証情報のキャッシュはOSのHTTP Basic Auth機構に頼らず、明示的に`Authorization: Basic ...`ヘッダーを毎回付与する実装を推奨（OkHttp等であれば`Authenticator`または固定ヘッダーで対応）
- ポーリング表示（ログ画面の自動更新など）は、Web UI自体も`/mqtt_bridge.log`・`/serial.log`を5秒間隔でポーリングしているのと同じ方式で問題ない
- `mqtt_host`に`.local`ホスト名は使用不可（本体のmDNS解決が`hosts: files dns`のみで`mdns4_minimal`非対応のため）。Home Assistant側のIPアドレス直指定が必要
- `web_port`を`80`にはできない（本体組み込みのnginxが使用中）
