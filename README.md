# Firebase Bandwith Scraper

この `app/` ディレクトリは Firebase Hosting + Cloud Run 構成をまとめたデプロイスイートです。Python 製スクレイパーを FastAPI 経由で公開し、UI は Firebase Hosting で配信されます。フォームで取得した結果は JSON として即時表示し、CSV はブラウザ内で生成してダウンロードできます。

## ディレクトリ構成

- `scraping.py` : Playwright を利用したスクレイピング本体。`scrape_as_json` で JSON を生成します。
- `backend/` : FastAPI アプリと Dockerfile。最大 5 並列ワーカーとキュー管理を実装しています。
- `public/` : Firebase Hosting に配信する静的 UI。
- `firebase.json` / `.firebaserc` : Hosting の設定とプロジェクト関連ファイル。
- `scripts/configure_firebase.py` : プロジェクト ID と Cloud Run の情報を入力して設定ファイルを自動更新するヘルパー。

> 以下のコマンドは `app/` ディレクトリに移動してから実行してください。

```bash
cd app
```

## 初期セットアップ

1. Firebase 設定ファイルを自動更新します。
   ```bash
   python scripts/configure_firebase.py
   ```
   プロンプトに従って Firebase プロジェクト ID、Cloud Run サービス ID、利用リージョンを入力してください。
2. Python の依存関係をインストールします。
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows の場合
   pip install -r backend/requirements.txt
   playwright install chromium
   ```

## ローカル開発

1. FastAPI サーバーを起動します。
   ```bash
   uvicorn backend.app:app --reload --port 8080
   ```
2. フロントエンドは任意の静的サーバーで `public/` を配信し、`/api` を `http://localhost:8080` にプロキシしてください。Firebase エミュレーターを使う場合は次のコマンドを実行します。
   ```bash
   npm install -g firebase-tools
   firebase emulators:start --only hosting
   ```
   必要であれば `firebase.json` の rewrite 先や `public/app.js` の `API_BASE` をローカル環境用に調整してください。

## Cloud Run へのデプロイ

1. コンテナイメージをビルドしてアップロードします。
   ```bash
   gcloud builds submit --tag gcr.io/PROJECT_ID/bandwith-scraper --file backend/Dockerfile .
   ```
2. Cloud Run にデプロイします。
   ```bash
   gcloud run deploy bandwith-scraper \
     --image gcr.io/PROJECT_ID/bandwith-scraper \
     --region asia-northeast1 \
     --allow-unauthenticated \
     --set-env-vars SCRAPER_MAX_CONCURRENCY=5,SCRAPER_ALLOWED_ORIGINS=https://your-hosting-domain
   ```
   主な環境変数:
   - `SCRAPER_MAX_CONCURRENCY` (デフォルト 5): 同時実行数の上限。
   - `SCRAPER_QUEUE_LIMIT` (デフォルト 50): キューに積めるジョブ数。超過時は HTTP 429。
   - `SCRAPER_MAX_HISTORY` (デフォルト `max(200, MAX_CONCURRENCY*20)`): メモリ上に保持する完了ジョブ数。
   - `SCRAPER_ALLOWED_ORIGINS`: CORS を許可するオリジン (カンマ区切り)。

## Firebase Hosting へのデプロイ

1. Firebase CLI のログインとプロジェクト選択。
   ```bash
   firebase login
   firebase use PROJECT_ID
   ```
2. 必要に応じて `firebase.json` の `serviceId` / `region` を確認・更新します。
3. ホスティングをデプロイします。
   ```bash
   firebase deploy --only hosting
   ```
   `/api/**` へのアクセスは Cloud Run 上の FastAPI にリライトされます。

## 運用メモ

- UI はジョブ状態をポーリングし、キュー待ちや実行中の件数を表示します。
- CSV はクライアント側で生成されるため、サーバー側にファイルを残しません。
- `scraping.py` の `scrape_as_json` を再利用すれば追加 API やバッチ処理を容易に構築できます。
