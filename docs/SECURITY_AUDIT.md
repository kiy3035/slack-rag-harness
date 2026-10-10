# Git 민감정보 점검 기록

## 점검 범위

- 원격 참조를 갱신한 뒤 전체 Git 이력 33개 커밋 검사
- 현재 브랜치와 원격 브랜치뿐 아니라 로컬 Reflog가 가리키는 커밋 검사
- `git fsck`로 확인한 도달 불가능 Blob 검사
- 현재 추적 파일과 작업 트리 검사
- 실제 `.env`의 추적·커밋 이력과 `.gitignore` 적용 여부 검사

검사 과정에서 값 자체는 출력하지 않고 규칙별 일치 파일과 위치 유형만 확인했다.

## 검사한 주요 형식

- Slack `xox*` Token과 32자리 Signing Secret
- GitHub Token과 Personal Access Token
- OpenAI 형식 API Key
- AWS Access Key
- Google API Key
- Vercel 형식 Key
- PEM·OpenSSH 등 Private Key Header
- Cloudflare Quick Tunnel 실제 주소
- Slack Workspace 주소, 개인 이름·이메일·로컬 Windows 사용자 경로
- `password`, `token`, `secret`, `api_key` 형태의 하드코딩 후보

## 결과

- 실제 Slack·GitHub·OpenAI·Vercel Token과 Slack Signing Secret은 발견되지 않았다.
- Private Key는 발견되지 않았다.
- 실제 `.env`는 전체 Git 이력에서 추적된 적이 없고 현재도 `.gitignore`가 적용된다.
- 실제 Cloudflare Quick Tunnel 주소, 개인 Slack Workspace 이름·도메인, 사용자 이름과 로컬 사용자 경로는 추적 파일에서 발견되지 않았다.
- 테스트의 `integration-signing-secret`, 환경변수 참조, Compose의 로컬 fallback 비밀번호는 실제 Secret이 아닌 Fixture·로컬 예시로 확인했다.
- SVG에서 AWS·Google Key 형식과 우연히 일치한 문자열은 모두 내장 PNG의 Base64 데이터 안에 있었고 평문 Key가 아니었다.
- Git 커밋 작성자 메타데이터에는 GitHub 비공개 주소가 아닌 개인 이메일을 사용한 과거 커밋이 있다. 이는 인증 Secret은 아니지만 공개 저장소에서 보일 수 있는 개인 정보다.

## 제한 사항

전용 Secret Scanner가 설치되어 있지 않아 저장소의 실제 이력과 알려진 키 형식을 직접 검사했다. 공급자가 새로 도입한 형식이나 임의 형식의 고엔트로피 문자열까지 완전하게 보장하는 검사는 아니다. 새 외부 자격 증명을 추가할 때는 해당 공급자 형식을 검사 규칙에 함께 추가해야 한다.

과거 커밋 작성자 이메일을 제거하려면 Git 이력 재작성과 강제 Push가 필요하므로 이번 점검에서는 변경하지 않았다. 향후 커밋에는 GitHub `noreply` 주소를 사용하는 것이 안전하다.
